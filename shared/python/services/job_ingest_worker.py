"""Sharded job-inventory ingestion worker (ADR-003).

What this replaces
------------------
Before this module, a single anonymous guest request drove the whole crawl
inline. `workday_jobs_service.run_workday_jobs_search` crossed the 1013-tenant
`WORKDAY_TENANT_CATALOG` (`workday_tenant_catalog.py:18`) with up to 6 search
terms (`workday_jobs_service.py:1194`), and `career_pages_service` added 1309
companies, for roughly 7,400 outbound requests to about 1,100 third-party hosts
inside one 110-second budget. About 9% of tenants were reached, and WHICH 9%
was nondeterministic, so two identical guests got different job boards.

Here the crawl is a scheduled background sweep over BOUNDED shards, writing to
the canonical `job_postings` inventory via `job_inventory_service`. The request
path reads inventory (`job_inventory_service.find_active_fresh`) and makes zero
outbound calls.

Why shards and not a queue
--------------------------
ADR-003 keeps ingestion on the EXISTING EventBridge rules
(`infra/terraform/schedulers.tf`) instead of adding SQS, because infra cost must
not increase. That costs us the visibility timeout, the DLQ and partial-batch
retries, so the equivalents live in Mongo (`job_source_state`):

* at-most-one worker per shard -> `inv.acquire_lease` (expiry = the visibility
  timeout; a crashed worker's shard is stealable with no reaper)
* ordered progress             -> `inv.advance_watermark` (monotonic, guarded)
* at-least-once safety         -> `inv.upsert_posting` is idempotent on
                                  (source, source_job_id); a duplicate Lambda
                                  delivery is a no-op update, and `first_seen_at`
                                  is `$setOnInsert` so it never moves
* failure isolation            -> `inv.record_failure` + exponential backoff,
                                  plus a `job_ingestion_runs` row. `run_shard`
                                  NEVER raises, so one poisoned shard cannot
                                  take down the tick.
* backpressure                 -> `inv.due_shards` excludes leased shards, so a
                                  tick whose predecessors are still running
                                  enqueues fewer, not more.

Shard sizing
------------
ADR-003's accepted trade-off is "a shard is the retry unit, so shards must stay
small enough that re-running one is cheap". Both registries are partitioned to
about 20 sources per shard (48 Workday shards, 64 career-page shards), which
bounds one shard re-run at roughly 20 listing calls plus a capped number of
detail calls - tens of seconds, not the 110-second cliff. Partitioning is by
stable hash of the source key, NOT by list index, so appending a tenant to the
catalog does not reshuffle every other tenant into a different shard (which
would invalidate every watermark at once).

Cost and safety rules enforced here
-----------------------------------
* Search is NEWEST-FIRST and stops at the guest freshness boundary (7 days,
  ADR-001). We never page past it, so an ingest costs pages-of-fresh-jobs, not
  pages-of-the-whole-board.
* A description is fetched and hashed ONCE. If the listing fields that feed
  `compute_content_hash` are unchanged and we already stored a body, the row
  gets a cheap `touch_seen` and no detail request at all.
* Requirement extraction never runs in this hot path. Changed bodies are queued
  by `content_hash`, deduped against `inv.find_parsed_requirements` first, so an
  unchanged JD is never re-parsed and a JD shared by two boards is parsed once.
* Sources are tagged `cost_class` on write. Both registries here are direct ATS
  (`free`); nothing paid is ever ingested by this worker, so ADR-004's guest
  exclusion is structural rather than configured.
* Outbound URLs are built from the SERVER-SIDE registries only. No payload field
  is ever used as a URL. Every request additionally passes a per-source host
  allowlist and the `jd_fetcher` SSRF guard.

Vendors arrive through `ats_providers`, not through this file (ADR-004)
----------------------------------------------------------------------
The four tables a vendor used to have to be hand-added to (`_TERMLESS_ATS`,
`_ATS_WITH_DETAIL`, `_HOST_ALLOW`, `_PROBE_HOST_ALLOW`) are now DERIVED from
`services.ats_providers.registry`, and listing routes through
`provider.list_jobs` when a provider is registered for a member's vendor. The
comment on `_TERMLESS_ATS` used to warn that forgetting one table ships a source
returning nothing; that failure is now structural rather than documented, which
is the whole point of the registry.

Two things about that routing are load-bearing:

* the allowlist NEVER depends on a successful provider import. It falls back to
  the literal, known-good lists below, because this is the SSRF boundary and it
  must fail toward "the old list", never toward "empty" or "allow all";
* `JOB_INGEST_USE_PROVIDERS` turns the new path off without a deploy, and
  `_list_workday` / `_list_career` stay as the fallback, because Workday and the
  seven board vendors are live and working today.

Pacing is keyed on the VENDOR, not the hostname
-----------------------------------------------
`HostPolicy` used to space requests per hostname, which is right for Greenhouse
(every tenant shares one API host) and wrong for iCIMS: every tenant is its own
hostname (`careers-<slug>.icims.com`) but the anti-bot wall counts per SOURCE
IP across all of them. Measured (ADR-004 §3.2): six concurrent requests spread
over sixty distinct iCIMS hostnames returned 405 "Human Verification" on all
sixty. The key therefore comes from `ats_registry.pacing_key_for(vendor, host)`
whenever a vendor is known, and falls back to the hostname otherwise.

Concurrency shape (deliberately not the old shape)
--------------------------------------------------
`guest_service.py:1159-1194` calls `shutdown(wait=False, cancel_futures=True)`,
but the callables it is cancelling sit inside their own `with
ThreadPoolExecutor(...)` blocks, and `__exit__` is `shutdown(wait=True)`. So
about 80 threads keep running past the deadline and can thaw into the next
invocation on a warm container. `run_bounded` below therefore: never uses a
`with` block on an executor, gives every task a cooperative stop check as its
first statement, clamps each request timeout to the remaining budget, and
cancels plus `shutdown(wait=False)` in a `finally`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from datetime import datetime, timedelta, timezone
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Set, Tuple)
from urllib.parse import urlparse

from services import job_inventory_service as inv

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


SOURCE_WORKDAY = "workday"
SOURCE_CAREER = "career-pages"
# Oracle ORC and iCIMS get their OWN sources rather than joining career-pages
# because a source is the unit of shard sizing and durable backoff, and both
# cost radically more per member than a board API does: an ORC pod can hold
# 7,381 requisitions (37 pages at a 15s timeout) and an iCIMS tenant is capped
# at one connection with 2.5s spacing. Sharing career-pages' 64 shards would let
# one Oracle pod eat a shard that also owed twenty Greenhouse boards.
#
# Taleo deliberately does NOT get one, and shares career-pages via
# `source_of_vendor`. It is a shrinking surface (ADR-004 §3.3: five of six
# probed zones are NXDOMAIN) and every endpoint needs a portal id we cannot
# derive, so a dedicated shard set would be almost entirely empty shards that
# `dispatch_due_shards` still pays a state upsert for on every tick.
SOURCE_ORACLE = "oracle"
SOURCE_ICIMS = "icims"
SOURCES = (SOURCE_WORKDAY, SOURCE_CAREER, SOURCE_ORACLE, SOURCE_ICIMS)

# ~21 tenants / ~20 companies per shard. See the module docstring: the shard is
# the retry unit under ADR-003, so it is sized for a cheap re-run, not for
# throughput. The two new counts are sized from COST PER MEMBER rather than
# from today's member count (which is a handful of seeds until discovery fills
# `company_ats_endpoints`): at ADR-004's G1/G2 targets these keep a shard at
# roughly one ORC pod and ~5 iCIMS tenants, which is what fits in 90 seconds.
SHARD_COUNTS: Dict[str, int] = {
    SOURCE_WORKDAY: _env_int("JOB_INGEST_WORKDAY_SHARDS", 48),
    SOURCE_CAREER: _env_int("JOB_INGEST_CAREER_SHARDS", 64),
    SOURCE_ORACLE: _env_int("JOB_INGEST_ORACLE_SHARDS", 48),
    SOURCE_ICIMS: _env_int("JOB_INGEST_ICIMS_SHARDS", 96),
}

# ADR-001: the guest boundary. Listing stops here; nothing older is paged for.
FRESHNESS_BOUNDARY_DAYS = _env_int("JOB_INGEST_MAX_AGE_DAYS", 7)

# Wall-clock budget for ONE shard run. Well inside the Lambda ceiling, and the
# lease TTL (900s) is deliberately longer so a lease cannot lapse mid-run.
SHARD_BUDGET_SECONDS = _env_float("JOB_INGEST_SHARD_BUDGET", 90.0)
LISTING_WORKERS = _env_int("JOB_INGEST_LISTING_WORKERS", 6)
DETAIL_WORKERS = _env_int("JOB_INGEST_DETAIL_WORKERS", 6)
MAX_DETAIL_FETCHES = _env_int("JOB_INGEST_MAX_DETAILS", 120)
MAX_LISTING_PAGES = _env_int("JOB_INGEST_MAX_PAGES", 5)
LISTING_PAGE_SIZE = 20
REQUEST_TIMEOUT = _env_float("JOB_INGEST_REQUEST_TIMEOUT", 8.0)
DETAIL_TIMEOUT = _env_float("JOB_INGEST_DETAIL_TIMEOUT", 6.0)

# Route listing through `ats_providers` when a provider is registered for the
# member's vendor. ON by default; the flag exists so the live Workday and
# career-pages crawl can be reverted to `_list_workday` / `_list_career` from
# the console without a deploy.
USE_PROVIDERS = _env_flag("JOB_INGEST_USE_PROVIDERS", True)

# Wall-clock ceiling on ONE member's listing when it goes through a provider.
# Without it a single slow vendor consumes the shard: iCIMS paces at 2.5s with
# max_concurrency=1 and pages up to 25 times, which is 65 seconds of a 90-second
# budget for one tenant, and every other member of the shard would get nothing.
# A member cut short is a resumable partial, which ADR-003 already permits.
MEMBER_BUDGET_SECONDS = _env_float("JOB_INGEST_MEMBER_BUDGET", 30.0)

# Inline detail fetches one member may spend (from the shard-wide `max_details`
# budget). Only ever spent on rows the LISTING left undated - see
# `_list_via_provider`.
MAX_INLINE_DETAILS_PER_MEMBER = _env_int("JOB_INGEST_INLINE_DETAILS", 20)

# Ceiling on `company_ats_endpoints` rows read per vendor when building a
# registry. The collection is designed to grow without bound; a shard must not.
MONGO_ENDPOINTS_PER_VENDOR = _env_int("JOB_INGEST_MONGO_ENDPOINTS", 20000)

# Per-host politeness. Career pages concentrate on a handful of ATS hosts
# (686 of 1309 companies are Greenhouse), so one shard can hit
# boards-api.greenhouse.io ~20 times; the limiter spaces those out.
HOST_MIN_INTERVAL = _env_float("JOB_INGEST_HOST_INTERVAL", 0.15)
HOST_JITTER = _env_float("JOB_INGEST_HOST_JITTER", 0.10)
HOST_FAILURE_THRESHOLD = _env_int("JOB_INGEST_HOST_FAILURES", 4)
RETRY_ATTEMPTS = _env_int("JOB_INGEST_RETRY_ATTEMPTS", 2)
RETRY_BASE_SECONDS = _env_float("JOB_INGEST_RETRY_BASE", 0.4)

# How many shards one scheduled tick enqueues. This is the concurrency bound on
# self-invoked ingest Lambdas; there is no queue to throttle us.
MAX_SHARDS_PER_TICK = _env_int("JOB_INGEST_SHARDS_PER_TICK", 16)

# Requirement extraction spends Bedrock tokens, so it is OFF unless explicitly
# enabled. It is never called from a listing/detail path either way.
REQUIREMENTS_ENABLED = _env_flag("JOB_INGEST_REQUIREMENTS_ENABLED", False)
REQUIREMENTS_BATCH = _env_int("JOB_INGEST_REQUIREMENTS_BATCH", 25)

SYSTEM_ACTOR = "system@internal"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_owner() -> str:
    """Lease owner id. The Lambda log stream is unique per container, so two
    warm containers racing the same shard get different owners and exactly one
    wins `acquire_lease`."""
    stream = os.environ.get("AWS_LAMBDA_LOG_STREAM_NAME") or "local"
    return f"{stream[:60]}:{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Registries -> members -> shards
# ---------------------------------------------------------------------------
# A "member" is one crawlable source unit (a Workday tenant, or a career-page
# company). `member_key` is server-derived, stable, and colon-free, because it
# is the PREFIX of every `source_job_id` we write:
#
#     source_job_id = "<member_key>:<raw_id>"
#
# That prefix is what lets a shard run decide which inventory rows it is
# responsible for closing without inventing a second shard->posting index.

_REGISTRY_LOCK = threading.Lock()
_REGISTRY_CACHE: Dict[str, List[Dict[str, Any]]] = {}

# The literals these tables USED to be, kept as the fallback the derived values
# below degrade to. Not dead code: they are the last known-good crawl, and the
# rule for every table derived from the provider registry is that a failed
# import must leave the existing sources working rather than emptying a table.
_LEGACY_TERMLESS_ATS: Tuple[str, ...] = (
    "greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee",
    "neogov")
_LEGACY_ATS_WITH_DETAIL: Tuple[str, ...] = ("greenhouse", "smartrecruiters",
                                            "workable")


def _ats_registry() -> Any:
    """The provider registry, or None when it cannot be imported.

    Every caller must handle None. One broken vendor module must not take job
    ingestion down, and the SSRF allowlist in particular must never widen or
    empty because an import failed.
    """
    try:
        from services.ats_providers import registry as ats_registry

        return ats_registry
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("ingest: ats_providers unavailable (%s: %s)",
                       type(exc).__name__, exc)
        return None


def _derived_tuple(attr: str, fallback: Tuple[str, ...]) -> Tuple[str, ...]:
    registry = _ats_registry()
    if registry is None:
        return fallback
    try:
        values = tuple(getattr(registry, attr)())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("ingest: %s() failed (%s)", attr, type(exc).__name__)
        return fallback
    # An empty answer means the registry loaded no providers at all, which is a
    # broken import rather than a real "no vendors" state.
    return values or fallback


# Vendors whose LIST endpoint returns the whole board newest-first with no
# search term. Only these may join the background sweep, because ADR-003's
# early-stop rule needs that ordering. Derived from `provider.termless`: a
# vendor used to have to be added here BY HAND, and the rows, the fetcher and
# the tests could all be perfect while the crawler never visited it.
_TERMLESS_ATS: Tuple[str, ...] = _derived_tuple("termless_vendors",
                                                _LEGACY_TERMLESS_ATS)

# Which vendors expose a per-posting detail endpoint. The rest ship the body in
# the listing payload, so they need no second request at all.
_ATS_WITH_DETAIL: Tuple[str, ...] = _derived_tuple("detail_vendors",
                                                   _LEGACY_ATS_WITH_DETAIL)

# Which SOURCE (i.e. which shard set and which durable backoff row) a vendor is
# swept under. The default is career-pages ON PURPOSE: a vendor registered
# tomorrow with no entry here is swept immediately under the shared source
# rather than silently never crawled, which is the exact trap the old
# hand-maintained `_TERMLESS_ATS` comment documented.
_VENDOR_SOURCE: Dict[str, str] = {
    "workday": SOURCE_WORKDAY,
    "oracle_orc": SOURCE_ORACLE,
    "icims": SOURCE_ICIMS,
}


def source_of_vendor(vendor: Any) -> str:
    return _VENDOR_SOURCE.get(_slug(vendor, 60), SOURCE_CAREER)


def _slug(value: Any, limit: int = 120) -> str:
    raw = str(value or "").strip().lower()
    out = []
    for ch in raw:
        out.append(ch if (ch.isalnum() or ch in "-_") else "-")
    return "-".join(part for part in "".join(out).split("-") if part)[:limit]


def _load_workday_members() -> List[Dict[str, Any]]:
    from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG

    seen: Dict[str, Dict[str, Any]] = {}
    for row in WORKDAY_TENANT_CATALOG:
        tenant = _slug(row.get("tenant"))
        if not tenant:
            continue
        # The catalog is generated; a duplicate tenant row would otherwise make
        # "covers the registry exactly once" false.
        seen.setdefault(tenant, {
            "member_key": tenant,
            "source": SOURCE_WORKDAY,
            "vendor": "workday",
            "tenant": tenant,
            "wd_host": _slug(row.get("wd_host")) or "wd1",
            "site": str(row.get("site") or "External"),
            "display_name": str(row.get("display_name") or tenant),
            "industry": str(row.get("industry") or "other"),
        })
    return list(seen.values())


def _load_career_members() -> List[Dict[str, Any]]:
    from services.career_pages_catalog import CAREER_PAGES_CATALOG

    seen: Dict[str, Dict[str, Any]] = {}
    for row in CAREER_PAGES_CATALOG:
        ats = _slug(row.get("ats"))
        slug = _slug(row.get("slug"))
        # The source check is not redundant with the termless check: `_TERMLESS_ATS`
        # now covers all eleven vendors, so without it a Workday or Oracle row
        # appearing in this catalog would be crawled as a career page and land in
        # `_list_career`, which has no branch for it.
        if not ats or not slug or ats not in _TERMLESS_ATS:
            continue
        if source_of_vendor(ats) != SOURCE_CAREER:
            continue
        key = f"{ats}-{slug}"
        seen.setdefault(key, {
            "member_key": key,
            "source": SOURCE_CAREER,
            "vendor": ats,
            "ats": ats,
            "slug": slug,
            "display_name": str(row.get("display_name") or slug),
            "industry": str(row.get("industry") or "other"),
        })

    # ENTERPRISE_CATALOG (Phenom / Eightfold / …) is a separate literal from
    # CAREER_PAGES_CATALOG. Without this loop those MNCs are interactive-only
    # even when a termless provider is registered for them.
    try:
        from services.career_pages_service import ENTERPRISE_CATALOG
    except Exception:
        ENTERPRISE_CATALOG = []
    for row in ENTERPRISE_CATALOG:
        ats = _slug(row.get("ats"))
        if ats == "orc":
            ats = "oracle_orc"
        slug = _slug(row.get("slug"))
        if not ats or not slug or ats not in _TERMLESS_ATS:
            continue
        if source_of_vendor(ats) != SOURCE_CAREER:
            continue
        member = _endpoint_member(
            ats, tenant=slug, site="",
            host=str(row.get("host") or "").strip().lower(),
            display_name=str(row.get("display_name") or slug),
            industry=str(row.get("industry") or "other"),
            extra=dict(row.get("extra") or {}))
        seen.setdefault(member["member_key"], member)
    return list(seen.values())


# Bootstrap endpoints for the two new sources.
#
# There is no Python catalog for Oracle or iCIMS and there is not meant to be
# one: under ADR-004 the registry is `company_ats_endpoints` in Mongo, filled by
# discovery. These are the ONLY endpoints with a recorded live verification
# (ADR-004 §3.1 for the two ORC pods and their reported `TotalJobsCount`;
# `scripts/probe_new_ats.py` for the iCIMS portal it pulled 50 postings from),
# and they exist so each new source is crawlable, and observably so, before
# discovery has run. `member_key` is minted exactly as `Endpoint.member_key`
# does, so a seed and its later Mongo row are the same member rather than two.
#
# Deliberately NOT seeded: Taleo. Every zone needs an opaque portal id that is
# not derivable and that we do not have for `tetratech`, and the provider
# reports that as a configuration gap rather than pretending the employer has no
# openings. A seed here would be a member that can only ever return zero.
_SEED_ENDPOINTS: Tuple[Dict[str, str], ...] = (
    {"vendor": "oracle_orc", "tenant": "jpmc", "site": "CX_1",
     "host": "jpmc.fa.oraclecloud.com", "display_name": "JPMorgan Chase",
     "industry": "banking"},
    {"vendor": "oracle_orc", "tenant": "eeho", "site": "CX_1",
     "host": "eeho.fa.us2.oraclecloud.com", "display_name": "Oracle",
     "industry": "tech"},
    # iCIMS seeds. Each was verified on 2026-08-18 to return real postings
    # through `IcimsProvider.list_jobs`, not merely to answer 200 - `*.icims.com`
    # is wildcard DNS, so "the host resolves" and even "the host serves HTML"
    # prove nothing about whether a tenant exists.
    #
    # The previous seed here was `us-careers-rivian`, and it is instructive:
    # Rivian has MIGRATED off iCIMS. That host still answers 200, with a
    # 143-byte body whose only content is `window.top.location.href =
    # 'https://careers.rivian.com/jobs'`. So the one seed the whole iCIMS source
    # had could never yield a posting, and the source would have measured as
    # "deployed and returning zero" rather than as "misconfigured".
    #
    # `careers-emory` is deliberately NOT here either: it is a landing page that
    # iframes the real board. `staff-emory` is where Emory's jobs are. A portal
    # that renders is not the same thing as a portal that hires.
    {"vendor": "icims", "tenant": "staff-emory", "site": "",
     "host": "staff-emory.icims.com", "display_name": "Emory University",
     "industry": "education"},
    {"vendor": "icims", "tenant": "careers-fremontbank", "site": "",
     "host": "careers-fremontbank.icims.com", "display_name": "Fremont Bank",
     "industry": "banking"},
    {"vendor": "icims", "tenant": "careers-fresenius-kabi", "site": "",
     "host": "careers-fresenius-kabi.icims.com",
     "display_name": "Fresenius Kabi", "industry": "pharma"},
    # Taleo, now that it is configurable. ADR-004 §3.3 declined to seed it
    # because "every zone needs an opaque portal id that is not derivable and
    # that we do not have"; `TaleoProvider._discover_portal` now reads that id
    # off the zone's own career-section page, so a bare zone is enough.
    {"vendor": "taleo", "tenant": "aarcorp", "site": "",
     "host": "aarcorp.taleo.net", "display_name": "AAR Corp",
     "industry": "aerospace"},
    {"vendor": "taleo", "tenant": "baesystems", "site": "",
     "host": "baesystems.taleo.net", "display_name": "BAE Systems",
     "industry": "aerospace"},
)


def _member_key_for(vendor: str, tenant: str, site: str = "") -> str:
    """`Endpoint.member_key`, recomputed here rather than imported.

    Must stay byte-identical to `ats_providers.base.Endpoint.member_key`: under
    ADR-003 this is the PREFIX of every `source_job_id` a board writes, so a
    divergence would fork one employer's inventory into two.
    """
    base = f"{vendor}-{tenant}"
    if site:
        base = f"{base}-{site}"
    return _slug(base, 140)


def _endpoint_member(vendor: str, *, tenant: str, site: str = "", host: str = "",
                     display_name: str = "", industry: str = "",
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One crawlable member, in the shape every listing path here expects.

    Carries the legacy per-source field names (`tenant`/`wd_host`/`site` for
    Workday, `ats`/`slug` for career pages) alongside the vendor-neutral ones, so
    `_list_workday` and `_list_career` keep working on a member that came out of
    Mongo rather than out of a catalog.
    """
    extra = dict(extra or {})
    member: Dict[str, Any] = {
        "member_key": _member_key_for(vendor, tenant, site),
        "source": source_of_vendor(vendor),
        "vendor": vendor,
        "ats": vendor,
        "tenant": tenant,
        "slug": tenant,
        "site": site,
        "host": host,
        "display_name": display_name or tenant,
        "industry": industry or "other",
        "extra": extra,
    }
    if vendor == "workday":
        member["wd_host"] = _slug(extra.get("wd_host")) or "wd1"
        member["site"] = site or "External"
    return member


def _load_seed_members(source: str) -> List[Dict[str, Any]]:
    return [_endpoint_member(row["vendor"], tenant=row["tenant"],
                             site=row.get("site") or "",
                             host=row.get("host") or "",
                             display_name=row.get("display_name") or "",
                             industry=row.get("industry") or "")
            for row in _SEED_ENDPOINTS
            if source_of_vendor(row["vendor"]) == source
            and row["vendor"] in _TERMLESS_ATS]


def _mongo_members(source: str) -> List[Dict[str, Any]]:
    """Members from `company_ats_endpoints`, or [] if that is unreachable.

    ADR-004 §4.4: `registry_members` reads the UNION of catalog and Mongo, so
    nothing regresses while the collection is empty or the cluster is down. This
    therefore never raises and never blocks - a crawl that cannot read the new
    registry must still crawl the old one.

    Queried per vendor so the `(vendor, status)` index does the work instead of a
    collection scan, and capped per vendor because that collection is designed to
    grow without bound while a shard is not.
    """
    vendors = [v for v in _TERMLESS_ATS if source_of_vendor(v) == source]
    if not vendors:
        return []
    try:
        from services import company_registry_service as company_registry
    except Exception as exc:
        logger.warning("ingest: company registry unavailable (%s)",
                       type(exc).__name__)
        return []

    out: List[Dict[str, Any]] = []
    for vendor in vendors:
        try:
            docs = list(company_registry.iter_endpoints(
                vendor=vendor, limit=MONGO_ENDPOINTS_PER_VENDOR))
        except Exception as exc:
            logger.warning("ingest: endpoint read failed for %s (%s)",
                           vendor, type(exc).__name__)
            continue
        for doc in docs:
            tenant = str(doc.get("tenant") or "").strip()
            if not tenant:
                continue
            out.append(_endpoint_member(
                vendor, tenant=tenant,
                site=str(doc.get("site") or "").strip(),
                host=str(doc.get("host") or "").strip().lower(),
                display_name=str(doc.get("display_name") or "").strip(),
                industry=str(doc.get("industry") or "").strip(),
                extra=doc.get("extra") or {}))
    return out


def _vendor_of(member: Dict[str, Any]) -> str:
    """The provider key for a member, whatever field it arrived under.

    Legacy Workday members name no vendor at all and legacy career members name
    it `ats`, so this is the one place that reconciles the three spellings.
    """
    return _slug(member.get("vendor")
                 or member.get("ats")
                 or ("workday" if member.get("source") == SOURCE_WORKDAY
                     else ""), 60)


def _identity_of(member: Dict[str, Any]) -> Tuple[str, str, str]:
    """(vendor, tenant, site), lowercased - the board's real identity.

    Deduping on `member_key` alone is not enough. The legacy Workday catalog
    keys a member on the bare tenant while `Endpoint.member_key` keys it on
    `workday-<tenant>-<site>`, so once `seed_from_catalogs` has run, the same
    board arrives twice under two different keys - and two keys means two
    `source_job_id` prefixes, i.e. every Workday posting duplicated.
    """
    tenant = str(member.get("tenant") or member.get("slug") or "").strip().lower()
    return (_vendor_of(member), tenant,
            str(member.get("site") or "").strip().lower())


def _load_members(source: str) -> List[Dict[str, Any]]:
    """Catalog members first, then anything Mongo adds that is genuinely new.

    Catalog order is preserved and catalog rows win every collision, so turning
    the new registry on cannot move or restate an existing member.
    """
    if source == SOURCE_WORKDAY:
        members = _load_workday_members()
    elif source == SOURCE_CAREER:
        members = _load_career_members()
    else:
        members = _load_seed_members(source)

    keys: Set[str] = {m["member_key"] for m in members}
    identities: Set[Tuple[str, str, str]] = {_identity_of(m) for m in members}
    for member in _mongo_members(source):
        identity = _identity_of(member)
        if member["member_key"] in keys or identity in identities:
            continue
        keys.add(member["member_key"])
        identities.add(identity)
        members.append(member)
    return members


def registry_members(source: str) -> List[Dict[str, Any]]:
    """Every crawlable member of a source registry, deduped by member_key.

    Cached at module scope after first use. The catalogs are large literal
    lists, so they are imported lazily (inside the loader) to keep this module
    out of the cold-start import graph of anything that merely references a
    constant here.
    """
    src = normalize_source(source)
    with _REGISTRY_LOCK:
        cached = _REGISTRY_CACHE.get(src)
        if cached is not None:
            return cached
    members = _load_members(src)
    with _REGISTRY_LOCK:
        _REGISTRY_CACHE[src] = members
    return members


def reset_registry_cache() -> None:
    """Test hook: drop the cached registries (they are otherwise process-wide)."""
    with _REGISTRY_LOCK:
        _REGISTRY_CACHE.clear()


def normalize_source(source: Any) -> str:
    src = _slug(source, 60)
    if src not in SOURCES:
        raise ValueError(f"unknown ingestion source: {source!r}")
    return src


def shard_count(source: str) -> int:
    return max(1, int(SHARD_COUNTS.get(normalize_source(source), 1)))


def shard_name(index: int) -> str:
    return f"s{int(index):03d}"


def all_shards(source: str) -> List[str]:
    return [shard_name(i) for i in range(shard_count(source))]


def _bucket(member_key: str, count: int) -> int:
    """Stable hash bucket.

    Deliberately NOT `index % count`: the catalogs are regenerated by a script,
    and inserting one row in the middle of a list-index partition would move
    almost every member to a different shard, invalidating every watermark and
    forcing a full re-crawl. blake2b of the member key is stable across
    processes and Python versions (unlike `hash()`, which is salted per process
    for str).
    """
    digest = hashlib.blake2b(member_key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % max(1, int(count))


def shard_of(source: str, member_key: str) -> str:
    return shard_name(_bucket(str(member_key or ""), shard_count(source)))


def normalize_shard(source: str, shard: Any) -> str:
    """Validate a shard id against the COMPUTED shard set.

    The shard arrives in a job payload, and a job payload is not a trusted
    channel, so it is checked against a server-generated set rather than merely
    sanitized. Nothing else in a payload is ever used to build a URL.
    """
    src = normalize_source(source)
    candidate = _slug(shard, 16)
    if candidate not in set(all_shards(src)):
        raise ValueError(f"unknown shard {shard!r} for source {src}")
    return candidate


def shard_members(source: str, shard: str) -> List[Dict[str, Any]]:
    src = normalize_source(source)
    target = normalize_shard(src, shard)
    count = shard_count(src)
    return [m for m in registry_members(src)
            if shard_name(_bucket(m["member_key"], count)) == target]


def registry_stats() -> Dict[str, Any]:
    """Operator view: registry size, shard count, and the largest shard (the
    thing that actually bounds a retry)."""
    out: Dict[str, Any] = {}
    for src in SOURCES:
        members = registry_members(src)
        count = shard_count(src)
        sizes: Dict[str, int] = defaultdict(int)
        for m in members:
            sizes[shard_name(_bucket(m["member_key"], count))] += 1
        out[src] = {
            "members": len(members),
            "shards": count,
            "max_shard_size": max(sizes.values()) if sizes else 0,
            "min_shard_size": min(sizes.values()) if sizes else 0,
            "empty_shards": count - len(sizes),
        }
    return out


# ---------------------------------------------------------------------------
# Outbound safety: server-built URLs, host allowlist, SSRF guard
# ---------------------------------------------------------------------------

# The literal, known-good lists. These are the FALLBACK, and the reason the
# derived tables below are built with `|` rather than `=`: this is the SSRF
# boundary, so a failed provider import must leave it at the old list, never at
# "empty" (which silently stops the live crawl) and never at "allow all".
_LEGACY_HOST_ALLOW: Dict[str, Tuple[str, ...]] = {
    SOURCE_WORKDAY: (".myworkdayjobs.com",),
    SOURCE_CAREER: (
        "boards-api.greenhouse.io",
        "api.lever.co",
        "api.ashbyhq.com",
        "api.smartrecruiters.com",
        "apply.workable.com",
        ".recruitee.com",
        "www.governmentjobs.com",
    ),
}


# Hosts the LIVENESS PROBE may touch. Deliberately a second list rather than an
# addition to `_HOST_ALLOW`: those are the machine-readable list APIs the crawl
# reads, these are the PUBLIC apply pages a candidate would click, and they are
# different hosts (`boards-api.greenhouse.io` vs `boards.greenhouse.io`).
# Widening the crawl list to cover probing would grow the fetch surface for no
# reason, so the two capabilities stay separately allowlisted.
_LEGACY_PROBE_HOST_ALLOW: Dict[str, Tuple[str, ...]] = {
    SOURCE_WORKDAY: (".myworkdayjobs.com",),
    SOURCE_CAREER: (
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
        "jobs.smartrecruiters.com",
        "careers.smartrecruiters.com",
        "apply.workable.com",
        ".recruitee.com",
        "www.governmentjobs.com",
    ),
}


def _derived_host_allow(legacy: Dict[str, Tuple[str, ...]],
                        attr: str) -> Dict[str, Tuple[str, ...]]:
    """Legacy hosts for each source, plus every host the providers declare.

    The added set is vendor-wide rather than per-source because a source is no
    longer a fixed vendor list: once members come from `company_ats_endpoints`,
    any provider's endpoint can turn up in the career-pages sweep (that is how
    Taleo is covered). Narrowing this back to per-vendor would be strictly
    tighter and is the obvious next refinement; it needs the guard to be handed
    the member's vendor everywhere, not just on the provider path.

    Every source gets a key, including the new ones, so `_allowed_by` never has
    to distinguish "no entry" from "empty entry".
    """
    registry = _ats_registry()
    added: Tuple[str, ...] = ()
    if registry is not None:
        try:
            added = tuple(getattr(registry, attr)())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("ingest: %s() failed (%s)", attr, type(exc).__name__)
    return {src: tuple(sorted(set(legacy.get(src, ())) | set(added)))
            for src in SOURCES}


_HOST_ALLOW: Dict[str, Tuple[str, ...]] = _derived_host_allow(
    _LEGACY_HOST_ALLOW, "api_hosts")
_PROBE_HOST_ALLOW: Dict[str, Tuple[str, ...]] = _derived_host_allow(
    _LEGACY_PROBE_HOST_ALLOW, "portal_hosts")


def _allowed_by(table: Dict[str, Tuple[str, ...]], source: str, url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    if (parsed.scheme or "").lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    for allowed in table.get(source, ()):
        if allowed.startswith("."):
            if host.endswith(allowed):
                return True
        elif host == allowed:
            return True
    return False


def probe_host_allowed(source: str, url: str) -> bool:
    return _allowed_by(_PROBE_HOST_ALLOW, source, url)


def assert_probe_url(source: str, url: str) -> str:
    """Probe-side twin of `assert_outbound_url`, same fail-closed posture.

    The canonical_url is the one URL in this module that came from a THIRD PARTY
    rather than being built from the server-side registry, so the SSRF guard
    here is load-bearing rather than defence in depth.
    """
    if not probe_host_allowed(source, url):
        raise ValueError("probe host not allowed for this source")
    from services.jd_fetcher import url_is_safe

    ok, reason = url_is_safe(url)
    if not ok:
        raise ValueError(f"probe url blocked: {reason}")
    return url


def host_allowed(source: str, url: str) -> bool:
    """Defence in depth. Every URL this module fetches is BUILT from the
    server-side registry, so this can only fail if a future edit starts taking
    a host from data. It is checked anyway, because that is exactly the kind of
    edit that turns a crawler into an SSRF primitive."""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    if (parsed.scheme or "").lower() != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    for allowed in _HOST_ALLOW.get(source, ()):
        if allowed.startswith("."):
            if host.endswith(allowed):
                return True
        elif host == allowed:
            return True
    return False


def assert_outbound_url(source: str, url: str) -> str:
    """Host allowlist + the shared SSRF guard (public-IP-only resolution).

    Fails CLOSED: a DNS failure or a name that resolves into private space is
    refused rather than fetched.
    """
    if not host_allowed(source, url):
        raise ValueError("outbound host not allowed for this source")
    from services.jd_fetcher import url_is_safe

    ok, reason = url_is_safe(url)
    if not ok:
        raise ValueError(f"outbound url blocked: {reason}")
    return url


# ---------------------------------------------------------------------------
# Per-host rate limiting + circuit breaking
# ---------------------------------------------------------------------------

class HostPolicy:
    """Per-host spacing and a per-host circuit breaker, scoped to ONE run.

    Deliberately in-process and per-run rather than in Mongo: a shard is a
    single Lambda invocation, so a run-scoped breaker is the whole blast radius,
    and a shared breaker would need a write per request. Cross-run failure
    memory is `job_source_state.next_attempt_at` (`inv.record_failure`), which
    is the durable half of the same idea.
    """

    def __init__(self, *, min_interval: float = HOST_MIN_INTERVAL,
                 jitter: float = HOST_JITTER,
                 failure_threshold: int = HOST_FAILURE_THRESHOLD):
        self._lock = threading.Lock()
        self._next_at: Dict[str, float] = {}
        self._failures: Dict[str, int] = defaultdict(int)
        self._min_interval = max(0.0, float(min_interval))
        self._jitter = max(0.0, float(jitter))
        self._threshold = max(1, int(failure_threshold))
        self.tripped: set = set()

    @staticmethod
    def host_of(url: str) -> str:
        try:
            return (urlparse(str(url or "")).hostname or "").lower()
        except Exception:
            return ""

    def is_open(self, host: str) -> bool:
        with self._lock:
            return host in self.tripped

    def acquire(self, host: str, *, deadline: float,
                stop: Optional[threading.Event] = None) -> bool:
        """Reserve the next slot for `host`. False means "do not call this host"
        (circuit open, out of budget, or asked to stop)."""
        if not host:
            return False
        now = time.monotonic()
        with self._lock:
            if host in self.tripped:
                return False
            earliest = max(now, self._next_at.get(host, 0.0))
            # Jitter prevents 20 Greenhouse companies in one shard from
            # marching in lockstep and looking like a burst.
            wait_until = earliest + self._min_interval
            if self._jitter:
                wait_until += random.uniform(0.0, self._jitter)
            self._next_at[host] = wait_until
        delay = earliest - now
        if delay <= 0:
            return True
        if earliest >= deadline:
            return False
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, end - time.monotonic()))
        return True

    def note_success(self, host: str) -> None:
        with self._lock:
            self._failures.pop(host, None)

    def note_failure(self, host: str) -> bool:
        """Returns True when this failure tripped the breaker."""
        with self._lock:
            self._failures[host] += 1
            if self._failures[host] >= self._threshold and host not in self.tripped:
                self.tripped.add(host)
                return True
        return False


def http_get_json(source: str, url: str, policy: HostPolicy, *,
                  deadline: float, stop: Optional[threading.Event] = None,
                  timeout: float = REQUEST_TIMEOUT,
                  method: str = "GET",
                  json_body: Optional[Dict[str, Any]] = None,
                  extra_headers: Optional[Dict[str, str]] = None) -> Optional[Any]:
    """One guarded outbound request with jittered retry.

    Returns the decoded JSON, or None for "give up on this URL" (blocked host,
    circuit open, out of budget, exhausted retries). Never raises for a network
    condition: a partial shard beats a failed shard, and the shard-level failure
    signal is recorded separately.
    """
    import requests

    assert_outbound_url(source, url)
    host = HostPolicy.host_of(url)
    attempts = max(1, int(RETRY_ATTEMPTS))
    for attempt in range(attempts):
        if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
            return None
        if not policy.acquire(host, deadline=deadline, stop=stop):
            return None
        # Clamp the socket timeout to the REMAINING budget. This is what stops a
        # task from outliving the run and thawing into the next invocation on a
        # warm container.
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            return None
        eff_timeout = max(0.5, min(float(timeout), remaining))
        headers = dict(_HTTP_HEADERS, **(extra_headers or {}))
        try:
            if method == "POST":
                resp = requests.post(url, json=json_body or {},
                                     headers=headers, timeout=eff_timeout)
            else:
                resp = requests.get(url, headers=headers, timeout=eff_timeout)
        except Exception as exc:  # requests.RequestException and friends
            policy.note_failure(host)
            logger.debug("ingest %s: network error on %s (%s)", source, host,
                         type(exc).__name__)
        else:
            if resp.status_code == 200:
                policy.note_success(host)
                try:
                    return resp.json()
                except ValueError:
                    return None
            # 4xx other than 429 is a permanent answer for this URL: the board
            # is gone or gated. Do not burn retries or trip the host breaker on
            # one dead company.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                return None
            policy.note_failure(host)
        if attempt + 1 < attempts:
            backoff = RETRY_BASE_SECONDS * (2 ** attempt)
            backoff += random.uniform(0.0, RETRY_BASE_SECONDS)
            if time.monotonic() + backoff >= deadline:
                return None
            time.sleep(backoff)
    return None


_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobPilotIngest/1.0)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# A public apply page is HTML and often bot-sensitive, so the probe does not
# claim to want JSON and does not send a JSON content type.
_PROBE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobPilotIngest/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}


def http_probe_status(source: str, url: str, policy: HostPolicy, *,
                      deadline: float, stop: Optional[threading.Event] = None,
                      timeout: float = REQUEST_TIMEOUT) -> Optional[int]:
    """HTTP status of one apply URL, or None for "could not find out".

    HEAD, not GET: the question is only whether the page still exists, and a
    HEAD costs the employer a fraction of a page render. Some ATSs answer HEAD
    with 405, in which case we retry once with GET and read the status only -
    the body is never downloaded into memory beyond the stream header.

    None is returned for every inconclusive condition (blocked host, circuit
    open, out of budget, timeout, network error). The caller must treat None as
    "no information", never as dead - which is the whole reason this returns an
    Optional instead of a bool.
    """
    import requests

    assert_probe_url(source, url)
    host = HostPolicy.host_of(url)
    if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
        return None
    if not policy.acquire(host, deadline=deadline, stop=stop):
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        return None
    eff_timeout = max(0.5, min(float(timeout), remaining))
    try:
        resp = requests.head(url, headers=_PROBE_HEADERS, timeout=eff_timeout,
                             allow_redirects=True)
        if resp.status_code == 405 or resp.status_code == 501:
            resp = requests.get(url, headers=_PROBE_HEADERS, timeout=eff_timeout,
                                allow_redirects=True, stream=True)
            resp.close()
    except Exception as exc:
        policy.note_failure(host)
        logger.debug("probe %s: network error on %s (%s)", source, host,
                     type(exc).__name__)
        return None
    policy.note_success(host)
    return int(resp.status_code)


# ---------------------------------------------------------------------------
# Bounded worker pool with REAL backpressure
# ---------------------------------------------------------------------------

class _Deadline(Exception):
    """Raised inside a task that started after the budget was already gone."""


def run_bounded(
    fn: Callable[[Any], Any],
    items: Iterable[Any],
    *,
    max_workers: int,
    deadline: float,
    stop: Optional[threading.Event] = None,
) -> List[Tuple[Any, Any, Optional[str]]]:
    """Run `fn` over `items` with at most `max_workers` concurrent calls.

    Returns [(item, result, error_or_None)] for every item that produced an
    outcome. Items never submitted (budget gone) are simply absent - a partial
    shard is a valid, resumable outcome under ADR-003.

    Three properties this has and the old guest fan-out did not:

    1. The submit window is bounded (`max_workers * 2` futures outstanding), so
       the executor's internal queue cannot grow to thousands of pending tasks.
       That IS the backpressure: we stop reading the registry when workers are
       busy.
    2. The executor is NEVER used as a context manager. `ThreadPoolExecutor
       .__exit__` is `shutdown(wait=True)`, which is precisely why
       `guest_service.py:1159-1194`'s `shutdown(wait=False, cancel_futures=True)`
       did nothing: the inner `with` blocks joined every thread anyway.
    3. Every task's first statement is a cooperative stop/deadline check, and
       every request timeout inside a task is clamped to the remaining budget,
       so tasks that already started still terminate promptly.
    """
    pool_items = list(items)
    results: List[Tuple[Any, Any, Optional[str]]] = []
    if not pool_items:
        return results

    # `stop` is a READ-ONLY external cancel signal owned by the caller (a shard
    # run reuses one event across its listing and detail phases). Teardown must
    # therefore fire an INTERNAL event: setting the caller's event here would
    # cancel the caller's next phase before it started.
    local_stop = threading.Event()

    def _cancelled() -> bool:
        return local_stop.is_set() or (stop is not None and stop.is_set())

    workers = max(1, min(int(max_workers), len(pool_items)))
    window = workers * 2

    def _task(item: Any) -> Any:
        if _cancelled() or time.monotonic() >= deadline:
            raise _Deadline()
        return fn(item)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jobingest")
    inflight: Dict[Any, Any] = {}
    iterator = iter(pool_items)
    exhausted = False
    try:
        while True:
            while (not exhausted and len(inflight) < window
                   and not _cancelled() and time.monotonic() < deadline):
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                inflight[executor.submit(_task, item)] = item
            if not inflight:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or _cancelled():
                break
            done, _pending = futures_wait(set(inflight), timeout=min(remaining, 1.0),
                                          return_when=FIRST_COMPLETED)
            for future in done:
                item = inflight.pop(future)
                try:
                    results.append((item, future.result(), None))
                except _Deadline:
                    results.append((item, None, "deadline"))
                except Exception as exc:
                    results.append((item, None, f"{type(exc).__name__}: {exc}"))
            if exhausted and not inflight:
                break
    finally:
        local_stop.set()
        for future in list(inflight):
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return results


# ---------------------------------------------------------------------------
# Listing discovery - newest first, stop at the freshness boundary
# ---------------------------------------------------------------------------

def _parse_iso(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for candidate in (text, text[:19], text[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


# Pay fields, by the names the six boards actually use. Deliberately a TOP-LEVEL
# lookup on a known list rather than handing the whole job dict to the parser
# and letting it hunt: that parser accepts generic range keys (`min`/`max`,
# `from`/`to`, `start`/`end`), and a job payload has unrelated fields by those
# names - `start` is a start date, not a floor - so a blind search would invent
# salaries. Absent here simply means the description fallback decides.
_PAY_KEYS = ("pay_input_ranges", "salaryRange", "salary_range", "compensation",
             "compensationTierSummary", "salary", "payRange", "pay")


def _pay_of(job: Dict[str, Any]) -> Any:
    if not isinstance(job, dict):
        return None
    for key in _PAY_KEYS:
        value = job.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _neogov_date(raw: Any) -> Optional[datetime]:
    """NeoGov publishes MM/DD/YY. Unparseable -> None, which the caller treats
    as "skip", never as "posted today" (ADR-001)."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%m/%d/%y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _listing_row(member: Dict[str, Any], raw_id: Any, *, title: str, url: str,
                 posted_at: Optional[datetime], location: Any,
                 description: str = "", employment_type: str = "",
                 posted_text: str = "", detail_ref: str = "",
                 compensation: Any = None) -> Dict[str, Any]:
    """One listing hit in the shape `inv.normalize_posting` consumes.

    `cost_class` is hard-coded `free`: both registries are direct ATS boards.
    ADR-004's guest exclusion is therefore structural - this worker has no code
    path that can write a `paid` row, so nothing metered can leak onto the
    anonymous path by configuration accident.
    """
    return {
        "source": member["source"],
        "source_job_id": f"{member['member_key']}:{raw_id}",
        "employer_id": _slug(member["display_name"], 120),
        "employer_name": member["display_name"],
        "company": member["display_name"],
        "title": (title or "").strip()[:300],
        "url": url,
        "locations": location if isinstance(location, (list, tuple)) else [location],
        "description": description or "",
        "employment_type": employment_type or "",
        # Whatever shape the board used, if anything. `salary_normalizer` reads
        # SHAPES rather than one vendor's field names, so a board that renames
        # its pay field still lands here, and one that publishes nothing simply
        # yields no band rather than a zero.
        "compensation": compensation,
        "posted_at": posted_at,
        "posted_at_confidence": inv.CONFIDENCE_EXACT if posted_at else inv.CONFIDENCE_UNKNOWN,
        "raw_posted_text": posted_text,
        "cost_class": inv.COST_CLASS_FREE,
        "source_quality": "direct",
        # Registry provenance. Carried onto the posting so the authenticated
        # feed tabs can filter by industry / company / ATS straight off the
        # inventory instead of re-joining against the catalogs at read time.
        "industry": member.get("industry") or "other",
        "ats": member.get("ats") or ("workday" if member["source"] == SOURCE_WORKDAY else ""),
        "tenant": member.get("slug") or member.get("tenant") or member["member_key"],
        "_detail_ref": detail_ref,
        "_member_key": member["member_key"],
    }


def _list_workday(member: Dict[str, Any], *, cutoff: datetime, policy: HostPolicy,
                  deadline: float, stop: Optional[threading.Event] = None,
                  now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Page one Workday tenant newest-first, stopping at the boundary.

    No search term. The old path crossed every tenant with up to 6 terms
    (`workday_jobs_service.py:1194`), which is where 6,078 of the ~7,400
    requests came from; an empty `searchText` returns the tenant's board in
    posted-date order, which is what a freshness crawl actually wants.

    Early stop: as soon as a page contains a row at or past the boundary, we
    stop - both for this page's remainder and for every later page. Because the
    feed is newest-first, everything after that point is by construction older
    than the guest window, so paging on would be pure waste.
    """
    from services.workday_cxs_fetcher import _approx_iso_from_relative

    now = now or _utcnow()
    tenant, host, site = member["tenant"], member["wd_host"], member["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    rows: List[Dict[str, Any]] = []

    for page in range(max(1, MAX_LISTING_PAGES)):
        body = {
            "searchText": "",
            "locations": [],
            "appliedFacets": {},
            "limit": LISTING_PAGE_SIZE,
            "offset": page * LISTING_PAGE_SIZE,
        }
        data = http_get_json(SOURCE_WORKDAY, url, policy, deadline=deadline,
                             stop=stop, method="POST", json_body=body)
        if not isinstance(data, dict):
            break
        postings = data.get("jobPostings") or []
        if not isinstance(postings, list) or not postings:
            break
        crossed_boundary = False
        for posting in postings:
            if not isinstance(posting, dict):
                continue
            title = (posting.get("title") or "").strip()
            if not title:
                continue
            posted_text = (posting.get("postedOn") or "").strip()
            posted_at = _parse_iso(_approx_iso_from_relative(posted_text, now=now))
            if posted_at is None:
                # ADR-001: an unknown date is never promoted to "today". The row
                # is skipped, and - importantly - it does NOT count as crossing
                # the boundary, since we cannot tell where it sits.
                continue
            if posted_at < cutoff:
                crossed_boundary = True
                break
            path = (posting.get("externalPath") or "").lstrip("/")
            req_id = (posting.get("bulletFields") or [None])[0] or path
            if not req_id:
                continue
            rows.append(_listing_row(
                member, req_id,
                title=title,
                url=f"{base}/{site}/{path}" if path else base,
                posted_at=posted_at,
                location=(posting.get("locationsText") or "").strip(),
                employment_type=(posting.get("timeType") or "").strip(),
                posted_text=posted_text,
                detail_ref=path,
            ))
        if crossed_boundary or len(postings) < LISTING_PAGE_SIZE:
            break
    return rows


def _list_career(member: Dict[str, Any], *, cutoff: datetime, policy: HostPolicy,
                 deadline: float, stop: Optional[threading.Event] = None,
                 now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """List one career-page company.

    These boards return the WHOLE board in a single unpaged call, so "stop at
    the boundary" is applied to the rows rather than to paging: everything older
    than the cutoff is dropped here, before any detail request is considered.
    That is where the cost is on this source - the listing call is one request
    per company either way, but the detail calls are per JOB.
    """
    ats, slug = member["ats"], member["slug"]
    rows: List[Dict[str, Any]] = []

    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline, stop=stop)
        for job in ((data or {}).get("jobs") or []) if isinstance(data, dict) else []:
            # first_published ONLY. ADR-001 removed the updated_at fallback:
            # Greenhouse is 686 of 1309 companies, so re-dating an edited req as
            # "posted today" was the single largest source of fake freshness.
            posted_at = _parse_iso(job.get("first_published"))
            if posted_at is None or posted_at < cutoff:
                continue
            rows.append(_listing_row(
                member, job.get("id"), title=job.get("title") or "",
                url=job.get("absolute_url") or "", posted_at=posted_at,
                location=((job.get("location") or {}).get("name")) or "",
                compensation=_pay_of(job)))

    elif ats == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline, stop=stop)
        for job in data if isinstance(data, list) else []:
            posted_at = _epoch_ms(job.get("createdAt"))
            if posted_at is None or posted_at < cutoff:
                continue
            rows.append(_listing_row(
                member, job.get("id"), title=job.get("text") or "",
                url=job.get("hostedUrl") or "", posted_at=posted_at,
                location=(job.get("categories") or {}).get("location") or "",
                # Lever ships the body in the listing, so this source needs no
                # detail request at all - but it is spread across four fields.
                description=_lever_body(job), compensation=_pay_of(job)))

    elif ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline, stop=stop)
        for job in ((data or {}).get("jobs") or []) if isinstance(data, dict) else []:
            if job.get("isListed") is False:
                continue
            posted_at = _parse_iso(job.get("publishedAt") or job.get("publishedDate"))
            if posted_at is None or posted_at < cutoff:
                continue
            rows.append(_listing_row(
                member, job.get("id"), title=job.get("title") or "",
                url=job.get("jobUrl") or "", posted_at=posted_at,
                location=job.get("location") or "",
                employment_type=str(job.get("employmentType") or ""),
                description=str(job.get("descriptionPlain") or ""),
                compensation=_pay_of(job)))

    elif ats == "smartrecruiters":
        url = (f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
               f"?limit=100&offset=0")
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline, stop=stop)
        for job in ((data or {}).get("content") or []) if isinstance(data, dict) else []:
            posted_at = _parse_iso(job.get("releasedDate") or job.get("createdOn"))
            if posted_at is None or posted_at < cutoff:
                continue
            loc = job.get("location") or {}
            rows.append(_listing_row(
                member, job.get("id"), title=job.get("name") or "",
                url=(job.get("ref") or {}).get("jobAd") or "", posted_at=posted_at,
                location=", ".join(str(loc.get(k) or "") for k in ("city", "region", "country")),
                detail_ref=str(job.get("id") or ""), compensation=_pay_of(job)))

    elif ats == "neogov":
        # POST, and the XHR header is required - a GET or a bare POST 404s.
        # `http_get_json` already speaks POST because Workday CXS needs it.
        url = f"https://www.governmentjobs.com/careers/{slug}/home/loadJobsOnMaps"
        # Measured: the body format is irrelevant (JSON or form both work) but
        # WITHOUT this header the endpoint returns 404, which reads as "no such
        # agency" rather than "you asked wrong".
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline, stop=stop,
                             method="POST", json_body={},
                             extra_headers={"X-Requested-With": "XMLHttpRequest"})
        for job in ((data or {}).get("jobList") or []) if isinstance(data, dict) else []:
            posted_at = _neogov_date(job.get("PostingDate"))
            if posted_at is None or posted_at < cutoff:
                continue
            job_id = job.get("ID")
            # `Classification` is the human title; `JobTitle` is a URL slug.
            title = str(job.get("Classification") or "").strip()
            if not job_id or not title:
                continue
            rows.append(_listing_row(
                member, job_id, title=title,
                url=f"https://www.governmentjobs.com/careers/{slug}/jobs/{job_id}",
                posted_at=posted_at,
                location=str(job.get("Location") or ""),
                employment_type=str(job.get("JobType") or ""),
                description=str(job.get("FullDescription") or ""),
                compensation=job.get("SalaryInfo")))

    elif ats == "workable":
        url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline, stop=stop)
        for job in ((data or {}).get("jobs") or []) if isinstance(data, dict) else []:
            posted_at = _parse_iso(job.get("published_on") or job.get("created_at"))
            if posted_at is None or posted_at < cutoff:
                continue
            shortcode = str(job.get("shortcode") or "")
            rows.append(_listing_row(
                member, shortcode or job.get("id"), title=job.get("title") or "",
                url=job.get("url") or "", posted_at=posted_at,
                location=", ".join(str(job.get(k) or "") for k in ("city", "state", "country")),
                detail_ref=shortcode))

    elif ats == "recruitee":
        url = f"https://{slug}.recruitee.com/api/offers/"
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline, stop=stop)
        for job in ((data or {}).get("offers") or []) if isinstance(data, dict) else []:
            posted_at = _parse_iso(job.get("published_at") or job.get("created_at"))
            if posted_at is None or posted_at < cutoff:
                continue
            rows.append(_listing_row(
                member, job.get("id"), title=job.get("title") or "",
                url=job.get("careers_url") or job.get("careers_apply_url") or "",
                posted_at=posted_at,
                location=", ".join(str(job.get(k) or "") for k in ("city", "state_code", "country")),
                description=str(job.get("description") or "")))

    # Newest-first ordering is asserted on OUR side for this source, because the
    # boards do not promise it. The detail budget is spent top-down, so the
    # freshest jobs get bodies first when a shard runs out of budget.
    rows.sort(key=lambda r: r["posted_at"] or cutoff, reverse=True)
    return rows


def _lever_body(job: Dict[str, Any]) -> str:
    """Assemble a Lever posting's full text from the four fields it spans.

    `descriptionPlain` alone is wrong twice over. It is EMPTY on a large share
    of real postings (7 of 17 on the first board checked), and even when
    present it holds only the intro blurb: the actual requirements live in
    `lists`, the structured "What You'll Do" / "What You'll Bring" sections,
    with `additionalPlain` carrying benefits and EEO text.

    Reading only `descriptionPlain` therefore stored ~60% of Lever postings
    with no body at all. Those rows extract no requirements, so they cannot be
    scored, cannot be classified beyond their title, and are excluded from
    results by the job-evidence gate - they occupy the inventory while being
    unservable.

    `lists[].content` is HTML, so it goes through the same text extractor the
    detail path uses. Section headings are kept: "What You'll Bring" is a
    strong signal for the required-vs-preferred split in
    `fit_score._split_sections`.
    """
    from services.career_pages_service import _html_to_text

    parts: List[str] = []
    for key in ("openingPlain", "descriptionPlain"):
        value = str(job.get(key) or "").strip()
        if value:
            parts.append(value)
    if not parts:
        # Some boards populate only the body variant.
        body = str(job.get("descriptionBodyPlain") or "").strip()
        if body:
            parts.append(body)

    for section in (job.get("lists") or []):
        if not isinstance(section, dict):
            continue
        heading = str(section.get("text") or "").strip()
        content = _html_to_text(str(section.get("content") or "")).strip()
        if heading and content:
            parts.append(f"{heading}\n{content}")
        elif content:
            parts.append(content)

    extra = str(job.get("additionalPlain") or "").strip()
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def _epoch_ms(value: Any) -> Optional[datetime]:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# Vendors whose listing already has a battle-tested path in this file.
# Routing those through providers would change live Workday/Greenhouse
# behaviour without rewriting the ingest tests — G8 forbids that. New
# vendors (oracle_orc, icims, jobvite, bamboohr, taleo) have NO legacy
# path, so they MUST go through the provider or they ingest nothing.
_LEGACY_LISTING_VENDORS = frozenset({
    "workday", "greenhouse", "lever", "ashby", "smartrecruiters",
    "workable", "recruitee", "neogov",
})


def _endpoint_from_member(member: Dict[str, Any]):
    from services.ats_providers.base import Endpoint

    vendor = _vendor_of(member)
    extra = dict(member.get("extra") or {})
    if vendor == "workday" and not extra.get("wd_host"):
        extra["wd_host"] = member.get("wd_host") or "wd1"
    return Endpoint(
        vendor=vendor,
        tenant=str(member.get("tenant") or member.get("slug") or ""),
        site=str(member.get("site") or ""),
        host=str(member.get("host") or "").strip().lower(),
        display_name=str(member.get("display_name") or ""),
        industry=str(member.get("industry") or "other"),
        extra=extra,
    )


def _provider_fetch_context(member: Dict[str, Any], *, cutoff: datetime,
                            deadline: float, stop: Optional[threading.Event]):
    """A FetchContext whose guard is THIS worker's SSRF allowlist.

    Providers must not grow a second outbound path. The crawler's host
    allowlist + `jd_fetcher.url_is_safe` stay load-bearing; the provider
    just speaks the vendor protocol on top.
    """
    import time as _time
    from services.ats_providers.base import FetchContext
    from services.ats_providers.http import PacedSession, make_fetchers

    source = str(member.get("source") or "")
    member_deadline = min(deadline, _time.monotonic() + MEMBER_BUDGET_SECONDS)

    def guard(_vendor: str, url: str) -> str:
        return assert_outbound_url(source, url)

    get_json, get_text, post_json = make_fetchers(
        PacedSession(), guard=guard, deadline=member_deadline, stop=stop)
    cutoff_iso = cutoff.astimezone(timezone.utc).isoformat() if cutoff else None
    return FetchContext(
        get_json=get_json, get_text=get_text, post_json=post_json,
        deadline=member_deadline, stop=stop, cutoff_iso=cutoff_iso,
        logger=logger,
    )


def _raw_job_to_listing(member: Dict[str, Any], job: Any, *,
                        cutoff: datetime) -> Optional[Dict[str, Any]]:
    posted_at = _parse_iso(getattr(job, "posted_at", "") or "")
    # Newest-first vendors (ORC) can stop paging at the boundary; HTML vendors
    # (iCIMS) cannot, so an undated row is kept rather than dropped. A dated
    # row past the cutoff is always dropped — that is ADR-001.
    if posted_at is not None and posted_at < cutoff:
        return None
    rid = str(getattr(job, "source_job_id", "") or "").strip()
    title = str(getattr(job, "title", "") or "").strip()
    if not rid or not title:
        return None
    detail = str(getattr(job, "detail_url", "") or "")
    return _listing_row(
        member, rid,
        title=title,
        url=str(getattr(job, "apply_url", "") or ""),
        posted_at=posted_at,
        location=getattr(job, "location", "") or "",
        description=str(getattr(job, "description", "") or ""),
        employment_type=str(getattr(job, "employment_type", "") or ""),
        posted_text=str(getattr(job, "raw_posted_text", "") or ""),
        detail_ref=detail or rid,
        compensation=getattr(job, "compensation", None),
    )


def _list_via_provider(member: Dict[str, Any], *, cutoff: datetime,
                       deadline: float,
                       stop: Optional[threading.Event] = None
                       ) -> Optional[List[Dict[str, Any]]]:
    """List through the registered provider, or None to fall back.

    None means "this vendor has no provider / the registry is down" — the
    caller then uses `_list_workday` / `_list_career`. An empty list means
    the provider ran and the board is empty, which must NOT fall through
    or a working iCIMS tenant would be re-fetched by a Greenhouse URL.
    """
    registry = _ats_registry()
    if registry is None:
        return None
    vendor = _vendor_of(member)
    provider = registry.get(vendor)
    if provider is None:
        return None
    endpoint = _endpoint_from_member(member)
    if not endpoint.tenant:
        return []
    ctx = _provider_fetch_context(member, cutoff=cutoff, deadline=deadline,
                                  stop=stop)
    rows: List[Dict[str, Any]] = []
    try:
        for job in provider.list_jobs(endpoint, ctx):
            if ctx.should_stop():
                break
            row = _raw_job_to_listing(member, job, cutoff=cutoff)
            if row is not None:
                rows.append(row)
    except Exception as exc:
        from services.ats_providers.base import ChallengeError
        if isinstance(exc, ChallengeError):
            logger.warning("ingest: %s challenged on %s (%s)",
                           vendor, endpoint.member_key, exc)
            return []
        logger.warning("ingest: provider %s failed for %s (%s: %s)",
                       vendor, endpoint.member_key, type(exc).__name__, exc)
        return []
    return rows


def _detail_via_provider(row: Dict[str, Any], member: Dict[str, Any], *,
                         deadline: float,
                         stop: Optional[threading.Event] = None) -> Optional[str]:
    registry = _ats_registry()
    if registry is None:
        return None
    vendor = _vendor_of(member)
    provider = registry.get(vendor)
    if provider is None or not getattr(provider, "has_detail", False):
        return None
    from services.ats_providers.base import RawJob

    endpoint = _endpoint_from_member(member)
    ctx = _provider_fetch_context(
        member, cutoff=datetime(1970, 1, 1, tzinfo=timezone.utc),
        deadline=deadline, stop=stop)
    raw_id = (str(row.get("_detail_ref") or "")
              or str(row.get("source_job_id") or "").rsplit(":", 1)[-1])
    job = RawJob(
        source_job_id=raw_id,
        title=str(row.get("title") or ""),
        apply_url=str(row.get("url") or ""),
        description=str(row.get("description") or ""),
        detail_url=str(row.get("_detail_ref") or ""),
    )
    try:
        detailed = provider.fetch_detail(endpoint, job, ctx)
    except Exception as exc:
        logger.debug("ingest: detail %s %s failed (%s)",
                     vendor, raw_id, type(exc).__name__)
        return ""
    if detailed is None:
        return ""
    return str(detailed.description or "")[:15000]


def list_member(member: Dict[str, Any], *, cutoff: datetime, policy: HostPolicy,
                deadline: float, stop: Optional[threading.Event] = None,
                now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    vendor = _vendor_of(member)
    if USE_PROVIDERS and vendor not in _LEGACY_LISTING_VENDORS:
        listed = _list_via_provider(member, cutoff=cutoff, deadline=deadline,
                                    stop=stop)
        if listed is not None:
            return listed
    if member.get("source") == SOURCE_WORKDAY:
        return _list_workday(member, cutoff=cutoff, policy=policy,
                             deadline=deadline, stop=stop, now=now)
    return _list_career(member, cutoff=cutoff, policy=policy,
                        deadline=deadline, stop=stop, now=now)


# ---------------------------------------------------------------------------
# Detail normalization - fetch a body at most once per changed posting
# ---------------------------------------------------------------------------

def _listing_implies_same_content(existing: Dict[str, Any], row: Dict[str, Any]) -> bool:
    """True when the LISTING fields prove `content_hash` cannot have moved.

    `inv.compute_content_hash` digests exactly: employer_id, normalized title,
    employment_type, location keys, description body. The first four are all
    visible in the listing payload, so when they match a stored doc that already
    HAS a body, re-fetching the body could only reproduce the same hash. That is
    the whole fetch-once rule: this check is what turns ~20 detail requests per
    company into ~0 on a steady-state re-run.

    "Already has a body" is `description_chars`, NOT `description_text`. The
    body now lives in S3 and the Mongo field is empty by design, so testing the
    text would make this return False for every posting on every crawl and
    silently re-fetch the entire registry every 15 minutes.
    """
    if not existing:
        return False
    if not (existing.get("description_chars")
            or existing.get("description_text")
            or existing.get("description_ref")):
        return False
    if existing.get("status") != inv.STATUS_ACTIVE:
        return False
    if existing.get("normalization_version") != inv.NORMALIZATION_VERSION:
        return False
    # The extractor contract counts too. `needs_parse` is only ever evaluated
    # inside `upsert_posting`, which an unchanged posting never reaches - so
    # without this line an EXTRACTOR_VERSION bump silently does nothing to the
    # existing inventory, which is the opposite of what that constant is for.
    if (existing.get("requirements") or {}).get("extractor_version") != inv.EXTRACTOR_VERSION:
        return False
    if existing.get("title_normalized") != inv.normalize_title(row.get("title")):
        return False
    if (existing.get("employment_type") or "") != _slug(row.get("employment_type"), 40):
        return False
    old_keys = sorted((l or {}).get("key", "") for l in (existing.get("locations") or []))
    new_keys = sorted(l["key"] for l in inv.normalize_locations(row.get("locations")))
    return old_keys == new_keys


def _needs_http_detail(row: Dict[str, Any], member: Dict[str, Any]) -> bool:
    """Would `fetch_detail` actually make a network request for this row?

    Mirrors `fetch_detail`'s own early-outs. Kept next to it deliberately: if
    one grows a new source the other must too, and the failure mode of drift is
    quiet (a source silently loses ingestion throughput), so they are reviewed
    together.
    """
    if row.get("description"):
        return False
    vendor = _vendor_of(member)
    if vendor not in _LEGACY_LISTING_VENDORS:
        registry = _ats_registry()
        provider = registry.get(vendor) if registry is not None else None
        if provider is not None:
            return bool(getattr(provider, "has_detail", False)
                        and (row.get("_detail_ref") or row.get("source_job_id")))
    if member.get("source") == SOURCE_WORKDAY:
        return bool(row.get("_detail_ref"))
    return member.get("ats") in _ATS_WITH_DETAIL


def fetch_detail(row: Dict[str, Any], member: Dict[str, Any], *, policy: HostPolicy,
                 deadline: float, stop: Optional[threading.Event] = None) -> str:
    """Fetch one posting body. Returns "" when unavailable - a body-less row is
    still worth storing (title/employer/date are the freshness surface), it just
    cannot be requirement-parsed."""
    if row.get("description"):
        return str(row["description"])
    source = member["source"]
    vendor = _vendor_of(member)
    if USE_PROVIDERS and vendor not in _LEGACY_LISTING_VENDORS:
        body = _detail_via_provider(row, member, deadline=deadline, stop=stop)
        if body is not None:
            return body

    if source == SOURCE_WORKDAY:
        path = row.get("_detail_ref") or ""
        if not path:
            return ""
        from services.workday_cxs_fetcher import _detail_html_to_text

        url = (f"https://{member['tenant']}.{member['wd_host']}.myworkdayjobs.com"
               f"/wday/cxs/{member['tenant']}/{member['site']}/{path}")
        data = http_get_json(SOURCE_WORKDAY, url, policy, deadline=deadline,
                             stop=stop, timeout=DETAIL_TIMEOUT)
        info = (data or {}).get("jobPostingInfo") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            return ""
        return _detail_html_to_text(str(info.get("jobDescription") or ""))[:15000]

    ats, slug = member.get("ats"), member.get("slug")
    if ats not in _ATS_WITH_DETAIL:
        return ""
    raw_id = str(row.get("_detail_ref") or "") or str(row["source_job_id"]).rsplit(":", 1)[-1]
    if not raw_id:
        return ""
    from services.career_pages_service import _html_to_text

    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{raw_id}"
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline,
                             stop=stop, timeout=DETAIL_TIMEOUT)
        return _html_to_text((data or {}).get("content") or "")[:15000] if isinstance(data, dict) else ""
    if ats == "smartrecruiters":
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{raw_id}"
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline,
                             stop=stop, timeout=DETAIL_TIMEOUT)
        if not isinstance(data, dict):
            return ""
        sections = ((data.get("jobAd") or {}).get("sections") or {})
        return " ".join(
            _html_to_text((sections.get(k) or {}).get("text") or "")
            for k in ("jobDescription", "qualifications", "additionalInformation")
        ).strip()[:15000]
    if ats == "workable":
        url = f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{raw_id}"
        data = http_get_json(SOURCE_CAREER, url, policy, deadline=deadline,
                             stop=stop, timeout=DETAIL_TIMEOUT)
        if not isinstance(data, dict):
            return ""
        return _html_to_text(
            f"{data.get('description') or ''} {data.get('requirements') or ''}")[:15000]
    return ""


# ---------------------------------------------------------------------------
# The shard run
# ---------------------------------------------------------------------------

def _close_stale_in_shard(source: str, listed_keys: set, *, cutoff_seen: datetime,
                          fresh_cutoff: datetime, run_id: str,
                          now: datetime, limit: int = 200) -> int:
    """Close postings this shard is responsible for that the run did not re-see.

    Two guards make this safe under the early-stop rule:

    * membership - only `source_job_id`s whose member-key prefix belongs to a
      member we LISTED SUCCESSFULLY this run. A member whose board timed out
      must not have its jobs closed.
    * age - only postings still inside the freshness window. Listing stops at
      the 7-day boundary, so an 8-day-old posting is legitimately not re-seen
      and closing it here would delete real inventory on every tick.
    """
    if not listed_keys:
        return 0
    candidates = inv.find_stale_for_verification(source, older_than=cutoff_seen,
                                                 limit=limit * 5)
    closed = 0
    for candidate in candidates:
        if closed >= limit:
            break
        source_job_id = str(candidate.get("source_job_id") or "")
        member_key = source_job_id.split(":", 1)[0]
        if member_key not in listed_keys:
            continue
        doc = inv.get_posting(source, source_job_id)
        if not doc:
            continue
        posted_at = doc.get("posted_at")
        if isinstance(posted_at, datetime):
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=timezone.utc)
            if posted_at < fresh_cutoff:
                continue  # aged out of the listing window, not actually gone
        if inv.close_posting(source, source_job_id, reason="not_seen", now=now):
            closed += 1
    if closed:
        inv.bump_run_counts(run_id, closed=closed)
    return closed


def run_shard(
    source: str,
    shard: str,
    *,
    owner: Optional[str] = None,
    now: Optional[datetime] = None,
    budget_seconds: Optional[float] = None,
    max_age_days: int = FRESHNESS_BOUNDARY_DAYS,
    listing_workers: int = LISTING_WORKERS,
    detail_workers: int = DETAIL_WORKERS,
    max_details: int = MAX_DETAIL_FETCHES,
) -> Dict[str, Any]:
    """Ingest ONE shard. Never raises.

    Phases, in order:
      1. lease         - `acquire_lease`; a second delivery of the same shard
                         while the first runs returns `skipped=lease_unavailable`
                         and touches nothing.
      2. listing       - bounded fan-out over the shard's members, newest-first,
                         stopping at the freshness boundary.
      3. triage        - rows whose listing fields prove the content_hash is
                         unchanged get a batched `touch_seen` and NO detail
                         request. Everything else queues for a body.
      4. detail        - bounded fan-out, capped at `max_details`, spent on the
                         freshest rows first.
      5. write         - sequential idempotent `upsert_posting`. Duplicate
                         delivery is a no-op update; `first_seen_at` never moves.
      6. parse-reuse   - a changed body whose content_hash was ALREADY parsed
                         (same JD on another board, or a revert) reuses the
                         stored requirements with zero Bedrock spend.
      7. progress      - monotonic watermark, closure of shard-owned rows,
                         success/failure state, run record.
    """
    started = time.monotonic()
    result: Dict[str, Any] = {"source": source, "shard": shard}
    try:
        src = normalize_source(source)
        shard_id = normalize_shard(src, shard)
    except ValueError as exc:
        # A bad payload is not a shard failure - there is no shard to back off.
        logger.warning("ingest: rejected job payload (%s)", exc)
        return {"source": str(source), "shard": str(shard), "error": "invalid_shard"}

    result.update({"source": src, "shard": shard_id})
    now = now or _utcnow()
    owner = owner or _default_owner()
    budget = float(budget_seconds if budget_seconds is not None else SHARD_BUDGET_SECONDS)
    deadline = started + max(1.0, budget)
    fresh_cutoff = now - timedelta(days=max(0, int(max_age_days)))

    # `now` is threaded through every state write so a run is deterministic:
    # the lease expiry, the backoff window and the posting timestamps must all
    # come from ONE clock reading, not from three separate `datetime.now()`
    # calls taken minutes apart inside a slow run.
    state = inv.acquire_lease(src, shard_id, owner,
                              ttl_seconds=inv.LEASE_TTL_SECONDS, now=now)
    if state is None:
        # Either another worker holds it (duplicate at-least-once delivery, or a
        # tick overlapping the previous one) or the shard is in backoff. Both
        # are correct no-ops, not errors.
        result.update({"skipped": "lease_unavailable", "counts": {}})
        return result

    run_id = inv.start_run(src, shard_id, trace_id=owner, now=now)
    members = shard_members(src, shard_id)
    policy = HostPolicy()
    stop = threading.Event()
    counts: Dict[str, int] = defaultdict(int)
    result.update({"run_id": run_id, "owner": owner, "members": len(members)})

    try:
        # --- 2. listing ------------------------------------------------------
        listed_keys: set = set()
        rows: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        listing = run_bounded(
            lambda m: list_member(m, cutoff=fresh_cutoff, policy=policy,
                                  deadline=deadline, stop=stop, now=now),
            members, max_workers=listing_workers, deadline=deadline, stop=stop)
        for member, member_rows, error in listing:
            if error:
                counts["errors"] += 1
                inv.record_run_failure(run_id, error, item=member["member_key"])
                continue
            listed_keys.add(member["member_key"])
            for row in member_rows or []:
                rows.append((member, row))
        counts["fetched"] += len(rows)
        inv.renew_lease(src, shard_id, owner, now=now)

        # --- 3. triage: which rows actually need a body ----------------------
        # ONE query for the whole shard, not one per row. A shard lists ~20
        # boards and several hundred postings, so per-row `get_posting` calls
        # meant several hundred sequential Atlas round trips inside a 90s
        # budget - the triage step alone could consume most of the shard's
        # wall clock and starve the detail fetches that actually add inventory.
        existing_by_id = inv.get_postings_map(
            src, [row["source_job_id"] for _m, row in rows])

        touch_ids: List[str] = []
        needs_detail: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for member, row in rows:
            existing = existing_by_id.get(row["source_job_id"])
            if _listing_implies_same_content(existing, row):
                touch_ids.append(row["source_job_id"])
                counts["unchanged"] += 1
            else:
                needs_detail.append((member, row))
        for start in range(0, len(touch_ids), 500):
            inv.touch_seen(src, touch_ids[start:start + 500], now=now)

        # --- 4. detail: bounded, freshest first ------------------------------
        # The budget exists to bound OUTBOUND REQUESTS, so only rows that would
        # actually make one may consume it. Lever, Ashby and Recruitee ship the
        # body in the listing payload, and a Workday row with no detail path has
        # nowhere to fetch from; charging those against `max_details` would
        # throttle ~450 of the 1309 career-page companies against a budget they
        # never spend, and silently drop the overflow (rows past the budget are
        # not written at all).
        needs_detail.sort(key=lambda pair: pair[1]["posted_at"] or fresh_cutoff,
                          reverse=True)
        free_rows = [p for p in needs_detail if not _needs_http_detail(p[1], p[0])]
        fetch_rows = [p for p in needs_detail if _needs_http_detail(p[1], p[0])]
        detail_targets = fetch_rows[:max(0, int(max_details))]
        counts["detail_skipped_budget"] += len(fetch_rows) - len(detail_targets)
        details = run_bounded(
            lambda pair: fetch_detail(pair[1], pair[0], policy=policy,
                                      deadline=deadline, stop=stop),
            detail_targets, max_workers=detail_workers, deadline=deadline, stop=stop)
        # Body-in-listing rows join the write set without a request.
        details = list(details) + [
            (pair, str(pair[1].get("description") or ""), None) for pair in free_rows
        ]
        counts["detail_free"] += len(free_rows)
        inv.renew_lease(src, shard_id, owner, now=now)

        # --- 5/6. write + parse reuse ---------------------------------------
        # Deadline-checked like the IO stages above. Writes are several database
        # round trips per posting, so a slow or distant cluster can make THIS
        # the stage that overruns: without the check a shard sailed past its 90s
        # budget and, in Lambda, would be killed mid-loop at the 300s function
        # timeout - losing the run record and holding the lease until its TTL.
        # Stopping early is safe by construction: the watermark only advances
        # over rows actually written, so the next tick resumes from there.
        watermark: Optional[datetime] = None
        parse_queue: List[Dict[str, str]] = []
        for (member, row), description, error in details:
            if time.monotonic() >= deadline or (stop is not None and stop.is_set()):
                counts["write_skipped_budget"] += 1
                continue
            if error:
                counts["errors"] += 1
                inv.record_run_failure(run_id, error, item=row["source_job_id"])
                continue
            payload = dict(row)
            payload.pop("_detail_ref", None)
            payload.pop("_member_key", None)
            if description:
                payload["description"] = description
            try:
                outcome = inv.upsert_posting(payload, run_id=run_id, now=now)
            except ValueError as exc:
                counts["errors"] += 1
                inv.record_run_failure(run_id, str(exc), item=row.get("source_job_id"))
                continue
            counts["upserted"] += 1
            if outcome["created"]:
                counts["created"] += 1
            if outcome["content_changed"]:
                counts["changed"] += 1
            else:
                counts["unchanged"] += 1
            posted_at = row.get("posted_at")
            if isinstance(posted_at, datetime):
                aware = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
                watermark = aware if watermark is None or aware > watermark else watermark
            if outcome["needs_parse"] and description:
                # Parse-once across the whole inventory: if this exact body was
                # already extracted (same req on a second board, or an edit that
                # reverted), reuse it instead of re-extracting.
                reusable = inv.find_parsed_requirements(outcome["content_hash"])
                if reusable:
                    if inv.mark_requirements_parsed(
                        src, row["source_job_id"], outcome["content_hash"],
                        reusable, now=now,
                    ):
                        counts["parsed_reused"] += 1
                        continue
                # Deterministic extraction, HERE, in the crawl. This is the
                # change that lets the request path score without ever loading a
                # JD body: `fit_score.extract_requirements` is pure regex over
                # the text, memoised in-process, and costs no Bedrock tokens, so
                # there is no reason to defer it to a queue. The LLM extractor
                # below stays an OPTIONAL enrichment rather than a prerequisite.
                if _persist_requirements(src, row["source_job_id"],
                                         outcome["content_hash"], description,
                                         row.get("title") or "", now=now):
                    counts["parsed_deterministic"] += 1
                elif REQUIREMENTS_ENABLED:
                    parse_queue.append({
                        "source": src,
                        "source_job_id": row["source_job_id"],
                        "content_hash": outcome["content_hash"],
                    })

        # Rows we never got to (budget) are simply not written. The next tick
        # picks them up; nothing is lost because the watermark only advances
        # over what we DID write.
        counts["parse_queued"] += len(parse_queue)

        # --- 7. progress -----------------------------------------------------
        if watermark is not None:
            inv.advance_watermark(src, shard_id, owner, watermark,
                                  checkpoint={"members_listed": len(listed_keys),
                                              "rows": len(rows)}, now=now)
        closed = _close_stale_in_shard(src, listed_keys, cutoff_seen=now,
                                       fresh_cutoff=fresh_cutoff, run_id=run_id,
                                       now=now)
        counts["closed"] += closed

        if members and not listed_keys:
            # Every member failed: that is a shard failure, and it must back off
            # rather than hammer the same dead hosts every tick.
            raise RuntimeError(
                f"all {len(members)} members failed to list "
                f"(tripped hosts: {len(policy.tripped)})"
            )

        inv.bump_run_counts(run_id, **{k: int(v) for k, v in counts.items()})
        inv.finish_run(run_id, status="succeeded", now=now)
        inv.record_success(src, shard_id, owner=owner, now=now)
        result.update({
            "counts": dict(counts),
            "listed_members": len(listed_keys),
            "parse_queue": parse_queue,
            "tripped_hosts": sorted(policy.tripped),
            "duration_ms": int((time.monotonic() - started) * 1000),
        })
        return result

    except Exception as exc:
        # Failure isolation: with no DLQ (ADR-003), backoff + the run record IS
        # the mechanism. One bad shard must never take down the tick, so this
        # returns rather than propagates.
        logger.error("ingest %s/%s failed: %s", src, shard_id, exc, exc_info=True)
        inv.bump_run_counts(run_id, **{k: int(v) for k, v in counts.items()})
        inv.record_run_failure(run_id, str(exc), item=shard_id)
        inv.finish_run(run_id, status="failed", now=now)
        inv.record_failure(src, shard_id, str(exc), owner=owner, now=now)
        result.update({"counts": dict(counts), "error": str(exc)[:300],
                       "duration_ms": int((time.monotonic() - started) * 1000)})
        return result

    finally:
        stop.set()
        inv.release_lease(src, shard_id, owner)


# ---------------------------------------------------------------------------
# Dispatch: EventBridge tick -> N shard jobs
# ---------------------------------------------------------------------------

def ensure_shard_state(source: str, *, now: Optional[datetime] = None) -> int:
    """Register every shard of a source in `job_source_state`.

    `ensure_source_state` is `$setOnInsert` only, so this is safe to call on
    every tick: it never disturbs a live lease, watermark or backoff.
    """
    src = normalize_source(source)
    shards = all_shards(src)
    for shard in shards:
        inv.ensure_source_state(src, shard, now=now)
    return len(shards)


def dispatch_due_shards(source: Optional[str] = None, *,
                        limit: int = MAX_SHARDS_PER_TICK,
                        now: Optional[datetime] = None,
                        enqueue: Optional[Callable[[str, Dict[str, Any]], str]] = None,
                        ) -> Dict[str, Any]:
    """Pick due shards and enqueue one async job each.

    This is the queue-receive replacement. `inv.due_shards` excludes shards that
    are LEASED or in BACKOFF, which gives real backpressure with no queue: if
    the previous tick's shards are still running, this tick enqueues fewer. The
    per-tick `limit` is the hard concurrency ceiling on self-invoked ingest
    Lambdas.

    Ordering is oldest-success-first so coverage is fair and deterministic
    rather than whatever Mongo returns, which is what made the old fan-out reach
    a nondeterministic 9% of tenants.
    """
    sources = [normalize_source(source)] if source else list(SOURCES)
    now = now or _utcnow()
    known: Dict[str, set] = {}
    for src in sources:
        ensure_shard_state(src, now=now)
        known[src] = set(all_shards(src))

    candidates: List[Dict[str, Any]] = []
    for src in sources:
        for state in inv.due_shards(src, limit=max(1, int(limit)) * 8, now=now):
            if state.get("shard") in known[src]:
                candidates.append(state)

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)

    def _sort_key(state: Dict[str, Any]):
        last = state.get("last_success_at")
        if isinstance(last, datetime):
            last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        else:
            last = epoch
        return (last, state.get("source", ""), state.get("shard", ""))

    candidates.sort(key=_sort_key)
    selected = candidates[:max(0, int(limit))]

    if enqueue is None:
        def enqueue(job_type: str, payload: Dict[str, Any]) -> str:
            from services.resume_service import ResumeService, get_resume_service
            job_id = get_resume_service().create_job(job_type, payload, SYSTEM_ACTOR)
            ResumeService.invoke_async(job_id, job_type, payload)
            return job_id

    enqueued: List[Dict[str, str]] = []
    for state in selected:
        payload = {"source": state["source"], "shard": state["shard"]}
        try:
            job_id = enqueue("job_ingest_shard", payload)
        except Exception as exc:
            logger.error("ingest dispatch failed for %s/%s: %s",
                         state.get("source"), state.get("shard"), exc)
            continue
        enqueued.append({**payload, "job_id": job_id})
    return {"sources": sources, "due": len(candidates), "enqueued": enqueued,
            "count": len(enqueued)}


# ---------------------------------------------------------------------------
# Maintenance + requirement extraction
# ---------------------------------------------------------------------------

def _persist_requirements(source: str, source_job_id: str, content_hash: str,
                          description: str, title: str, *,
                          now: Optional[datetime] = None) -> bool:
    """Extract and store the deterministic requirement profile for one posting.

    Best effort by design: a posting whose profile fails to store is still a
    perfectly good inventory row, it just costs a live extraction the first time
    somebody scores against it (`fit_score.score` falls back automatically).
    """
    try:
        from services import fit_score

        profile = fit_score.extract_requirements(description, title)
        if not profile:
            return False
        return inv.mark_requirements_parsed(source, source_job_id, content_hash,
                                            profile, now=now)
    except Exception as exc:  # pragma: no cover - never break a crawl
        logger.warning("ingest: requirement extraction failed for %s: %s",
                       str(source_job_id)[:40], type(exc).__name__)
        return False


def run_maintenance(*, now: Optional[datetime] = None,
                    purge_limit: int = 5000,
                    expire_limit: int = 5000,
                    verify_limit: int = 0,
                    probe_limit: int = 0,
                    probe_budget_seconds: float = 60.0) -> Dict[str, Any]:
    """Daily housekeeping: age out, close, purge, and re-verify.

    Order matters. Expiry closes first so the rows it closes are purge-eligible
    in the SAME sweep (they are stamped with zero retention days), which is what
    keeps the collection at a true rolling 7-day window instead of a 7-day
    window plus a growing tail of closed-but-not-yet-purged rows.

    `purge_expired` only deletes docs that are BOTH closed and past `purge_at`,
    and `purge_at` is only ever stamped by `close_posting`, so a live posting
    cannot be reached from here. Pinned postings are archived to S3 and verified
    present before deletion.
    """
    now = now or _utcnow()
    expired = inv.expire_aged_postings(limit=expire_limit, now=now)
    purged = inv.purge_expired(limit=purge_limit, now=now)
    result: Dict[str, Any] = {
        "expired": expired,
        "purged": purged.get("deleted", 0),
        "archived": purged.get("archived", 0),
        "retained_unarchived": purged.get("retained_unarchived", 0),
        "registry": registry_stats(),
    }
    if verify_limit:
        result["verified"] = verify_liveness(limit=int(verify_limit), now=now)
    # Off unless a limit is passed, so enabling outbound probing of employer
    # infrastructure is always an explicit decision at the call site.
    if probe_limit:
        result["probed"] = probe_liveness(limit=int(probe_limit), now=now,
                                          budget_seconds=probe_budget_seconds)
    return result


# Postings whose liveness has not been reconfirmed in this long are closed.
# A full crawl cycle is ~1h per source, so 72h is roughly 70 missed chances.
# Deliberately NOT tuned close to the cycle time: a single ATS having a bad day
# would then close every posting from 686 Greenhouse companies at once. Three
# days inside a seven-day window is the honest trade - long enough to ride out
# an outage, short enough that "we cannot confirm this is still open" stops
# being shown as a live match.
VERIFY_STALE_HOURS = _env_int("JOB_INGEST_VERIFY_STALE_HOURS", 72)


def verify_liveness(*, limit: int = 200, now: Optional[datetime] = None
                    ) -> Dict[str, int]:
    """Close postings the crawl has stopped seeing.

    `_close_stale_in_shard` already does this per shard, but only for shards that
    actually ran. A shard stuck in backoff (a board that is down, an ATS that
    rate-limited us) leaves its postings looking live indefinitely, and a dead
    link in a guest's top five is exactly the trust failure the freshness work
    exists to prevent. This is the backstop that does not depend on the shard
    making progress.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(hours=max(1, VERIFY_STALE_HOURS))
    out = {"checked": 0, "closed": 0}
    per_source = max(1, int(limit) // max(1, len(SOURCES)))
    for src in SOURCES:
        rows = inv.find_stale_for_verification(src, older_than=cutoff, limit=per_source)
        out["checked"] += len(rows)
        for row in rows:
            if inv.close_posting(src, row.get("source_job_id") or "",
                                 reason="unverified", now=now):
                out["closed"] += 1
    return out


# Only these mean "this posting is gone". Everything else - 200, any 3xx, 403
# (bot-walled), 429 (throttled), any 5xx, any network error - is inconclusive
# and closes nothing. An ATS that blocks us is a fact about us, not about the
# requisition.
PROBE_DEAD_STATUSES = (404, 410)
# Consecutive dead readings before closing. Two, not one: closing a live
# posting costs a candidate an application they would have made, while carrying
# a dead one for an extra maintenance tick costs one wasted click. A CDN or
# regional blip that 404s once does not survive two sweeps.
PROBE_STRIKES_TO_CLOSE = _env_int("JOB_INGEST_PROBE_STRIKES", 2)
# Circuit breaker for the CONCLUSION, not the connection. If most of what a host
# returns in one sweep reads dead, the likelier explanation is that the host
# changed its URL scheme or is serving 404 under load - not that every employer
# on it closed at once. This is the same worry that keeps VERIFY_STALE_HOURS
# far from the crawl cycle time, applied to a signal that acts much faster.
PROBE_MAX_DEAD_RATIO = _env_float("JOB_INGEST_PROBE_MAX_DEAD_RATIO", 0.5)
PROBE_MIN_HOST_SAMPLE = _env_int("JOB_INGEST_PROBE_MIN_HOST_SAMPLE", 8)
# Postings younger than this are never probed: several ATSs publish to their
# list API before the public detail page propagates, so a fresh posting can be
# genuinely 404 for minutes while being perfectly real.
PROBE_GRACE_HOURS = _env_int("JOB_INGEST_PROBE_GRACE_HOURS", 6)
PROBE_INTERVAL_HOURS = _env_int("JOB_INGEST_PROBE_INTERVAL_HOURS", 24)


def probe_liveness(*, limit: int = 200, now: Optional[datetime] = None,
                   budget_seconds: float = 60.0,
                   stop: Optional[threading.Event] = None) -> Dict[str, int]:
    """Ask apply URLs directly whether they still exist, and close the dead.

    Everything else in this module infers death from ABSENCE: `_close_stale_in_shard`
    when a listing stops returning a posting, `verify_liveness` when no crawl has
    confirmed one for days. Both are blind to the common case where an ATS keeps
    a requisition in its list API after the detail page has closed - the posting
    looks perfectly live to us and dead to the candidate who clicks it.

    Bounded on purpose: a slice per sweep, least-recently-probed first, inside a
    wall-clock budget and behind the same per-host spacing the crawl uses. This
    runs against employer infrastructure, so it stays polite before it stays
    fast.

    LEAVE THIS OFF - measured 2026-08-05
    ------------------------------------
    It was built on the assumption that employers routinely keep a CLOSED
    requisition on their list endpoint, so the posting looks live to the crawl
    and dead to whoever clicks Apply. That assumption was tested rather than
    trusted, against jobs the boards were listing at the time:

        Airbnb (greenhouse)          30 checked   0 dead
        State of Washington (neogov) 28 checked   0 dead
        City of Seattle (neogov)     29 checked   0 dead
        Workday, 40 tenants          35 checked   0 dead
        -----------------------------------------------
        TOTAL                       122 checked   0 dead

    Zero of 122 bounds the true rate below roughly 2.5%, and probably far
    lower. The existing signals - a posting vanishing from the list, and the
    72h `verify_liveness` backstop - are evidently catching closures already.

    Turning this on would therefore spend thousands of nightly requests against
    employer infrastructure to find almost nothing, which is a real cost paid
    for an imagined problem. It stays in the tree because it is also the
    instrument that produced the table above: if dead links are ever actually
    reported, run it on a sample and get a number before changing anything.

    Re-measure before enabling. Do not enable on the strength of this docstring.
    """
    now = now or _utcnow()
    deadline = time.monotonic() + max(1.0, float(budget_seconds))
    policy = HostPolicy()
    out = {"probed": 0, "dead": 0, "closed": 0, "inconclusive": 0, "skipped_host": 0}

    grace = now - timedelta(hours=max(0, PROBE_GRACE_HOURS))
    due = now - timedelta(hours=max(1, PROBE_INTERVAL_HOURS))
    per_source = max(1, int(limit) // max(1, len(SOURCES)))

    for src in SOURCES:
        try:
            rows = inv.find_for_liveness_probe(
                src, limit=per_source, first_seen_before=grace, probed_before=due)
        except Exception as exc:  # pragma: no cover - never break maintenance
            logger.warning("probe: selection failed for %s: %s", src, type(exc).__name__)
            continue

        # Decide per host only after the whole sweep, so the ratio guard sees a
        # sample instead of reacting to the first reading.
        pending: List[Tuple[str, str, str]] = []   # (host, source_job_id, url)
        dead_by_host: Dict[str, int] = defaultdict(int)
        seen_by_host: Dict[str, int] = defaultdict(int)

        for row in rows:
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                break
            url = str(row.get("canonical_url") or "")
            sjid = str(row.get("source_job_id") or "")
            if not url or not sjid:
                continue
            host = HostPolicy.host_of(url)
            try:
                status = http_probe_status(src, url, policy, deadline=deadline, stop=stop)
            except ValueError:
                # Not probe-allowlisted (or SSRF-refused). Says nothing about
                # the posting, so it must not count as a reading.
                continue
            out["probed"] += 1
            seen_by_host[host] += 1
            if status is None:
                out["inconclusive"] += 1
                continue
            if status in PROBE_DEAD_STATUSES:
                out["dead"] += 1
                dead_by_host[host] += 1
                pending.append((host, sjid, url))
            else:
                # A live reading is worth recording immediately: it resets any
                # strikes this posting had accumulated.
                inv.record_liveness_probe(src, sjid, alive=True, now=now)

        for host, sjid, url in pending:
            seen = seen_by_host.get(host, 0)
            ratio = dead_by_host.get(host, 0) / seen if seen else 0.0
            if seen >= PROBE_MIN_HOST_SAMPLE and ratio > PROBE_MAX_DEAD_RATIO:
                # Systemic, not per-posting. Record nothing - not even a strike -
                # because a strike here would close everything on this host on
                # the next sweep, which is precisely the outcome being avoided.
                out["skipped_host"] += 1
                logger.warning("probe: %s looks systemically dead (%d/%d), closing nothing",
                               host, dead_by_host.get(host, 0), seen)
                continue
            strikes = inv.record_liveness_probe(src, sjid, alive=False, now=now)
            if strikes >= PROBE_STRIKES_TO_CLOSE:
                if inv.close_posting(src, sjid, reason=inv.CLOSED_REASON_LINK_DEAD, now=now):
                    out["closed"] += 1
    return out


_REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "required_skills": {"type": "array", "items": {"type": "string"}},
        "preferred_skills": {"type": "array", "items": {"type": "string"}},
        "min_years_experience": {"type": ["number", "null"]},
        "education": {"type": "string"},
    },
    "required": ["required_skills", "preferred_skills"],
}

_REQUIREMENTS_SYSTEM = (
    "You extract hiring requirements from a job description. Return ONLY what "
    "the posting states. Never infer, never generalize, never add a skill the "
    "text does not mention. If the posting does not state a years-of-experience "
    "minimum, return null."
)


def extract_requirements(job_ids: Sequence[Dict[str, str]], *,
                         now: Optional[datetime] = None,
                         limit: int = REQUIREMENTS_BATCH) -> Dict[str, Any]:
    """Requirement extraction, ONE call per distinct changed content_hash.

    ADR-002 allows this to run asynchronously, cached by content hash. It is
    never called from listing or detail: those paths only queue hashes. Three
    layers keep the spend bounded:

    * dedupe by content_hash within the batch, so a req posted by 30 tenants
      costs one call;
    * `find_parsed_requirements` first, so a hash already extracted anywhere in
      inventory costs zero calls;
    * `mark_requirements_parsed` carries the hash in its FILTER, so an
      extraction that lands after the JD changed again is discarded rather than
      stamped onto fresh text.

    Off unless `JOB_INGEST_REQUIREMENTS_ENABLED` is set: it is the only part of
    ingestion that spends Bedrock tokens.
    """
    now = now or _utcnow()
    if not REQUIREMENTS_ENABLED:
        return {"skipped": "requirements_disabled", "parsed": 0, "reused": 0}

    by_hash: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for item in list(job_ids or [])[: max(1, int(limit)) * 20]:
        content_hash = str((item or {}).get("content_hash") or "")
        source = str((item or {}).get("source") or "")
        source_job_id = str((item or {}).get("source_job_id") or "")
        if content_hash and source and source_job_id:
            by_hash[content_hash].append({"source": source,
                                          "source_job_id": source_job_id})

    parsed = reused = failed = 0
    for content_hash in list(by_hash)[: max(1, int(limit))]:
        targets = by_hash[content_hash]
        requirements = inv.find_parsed_requirements(content_hash)
        if requirements is None:
            docs = inv.find_by_content_hash(content_hash, limit=1)
            body = ""
            if docs:
                full = inv.get_posting(docs[0]["source"], docs[0]["source_job_id"])
                body = str((full or {}).get("description_text") or "")
            if not body:
                continue
            try:
                from services.claude_client import CLAUDE_FLASH, claude_json

                requirements = claude_json(
                    prompt=(
                        "Extract the stated hiring requirements from this job "
                        "description.\n\n<job_description>\n"
                        f"{body[:20000]}\n</job_description>"
                    ),
                    system=_REQUIREMENTS_SYSTEM,
                    schema=_REQUIREMENTS_SCHEMA,
                    model=CLAUDE_FLASH,
                    max_tokens=1500,
                    temperature=0.0,
                )
            except Exception as exc:
                failed += 1
                logger.warning("ingest requirements extraction failed: %s",
                               type(exc).__name__)
                continue
            if not isinstance(requirements, dict):
                failed += 1
                continue
            parsed += 1
        else:
            reused += 1
        for target in targets:
            inv.mark_requirements_parsed(target["source"], target["source_job_id"],
                                         content_hash, requirements, now=now)
    return {"parsed": parsed, "reused": reused, "failed": failed,
            "hashes": len(by_hash)}
