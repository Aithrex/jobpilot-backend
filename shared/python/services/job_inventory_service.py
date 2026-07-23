"""Canonical job inventory - the single stable identity for a job posting.

ADR-003 (docs/adr/2026-07-21-guest-discovery-freshness-and-fit.md) records the
context: today there are FIVE mutually-unaware per-query caches (`jobs_cache`,
`workday_jobs_cache`, `career_pages_cache`, `mnc_pages_cache`,
`apify_actor_cache`). The same posting is stored once per distinct query hash,
keyed on search terms that include resume-derived skills, so the key is
effectively resume-specific and almost never collides between two users. There
is no stable job identity anywhere in the system.

This module is that identity. Four collections:

* ``job_postings``       - one doc per (source, source_job_id). UNIQUE.
* ``job_source_state``   - one doc per (source, shard): watermark, cursor/ETag,
                           lease, retry/backoff.
* ``job_ingestion_runs`` - one doc per shard run: counts, duration, failures.
* ``job_match_snapshots``- ALREADY EXISTS (guest_service, spec 1.4). We only ADD
                           references here; nothing existing is redesigned.

Why leases and watermarks live in Mongo and not in a queue
----------------------------------------------------------
ADR-003 deliberately schedules ingestion on the EXISTING EventBridge rules
(`infra/terraform/schedulers.tf:12-36`, `local.scheduled_jobs` -> resume
Lambda) rather than introducing SQS, because infra cost must not increase.
There is therefore no visibility timeout, no DLQ and no partial-batch response.
Bounded concurrency, at-most-one-worker-per-shard, ordered progress and
at-least-once safety are implemented HERE, in `job_source_state`:

* concurrency bound  -> ``acquire_lease`` (guarded ``find_one_and_update``)
* ordered progress   -> ``advance_watermark`` (monotonic, never moves backward)
* at-least-once      -> ``upsert_posting`` is idempotent on (source,
                        source_job_id); duplicate delivery is a no-op update
* failure isolation  -> ``record_failure`` + exponential backoff in
                        ``next_attempt_at``, plus a `job_ingestion_runs` row

Parse-once semantics
--------------------
``content_hash`` is a digest of the fields that change what the posting MEANS
(employer, title, description, locations, employment type). Expensive
requirement extraction (ADR-002: "Expensive requirement extraction may run
asynchronously once per changed JD, cached by content hash") runs only when
``requirements.source_content_hash != content_hash`` or the extractor version
moved. ``mark_requirements_parsed`` carries the hash it parsed IN THE FILTER, so
a slow parse that lands after the JD changed again cannot overwrite fresh data.

Retention invariant
-------------------
``purge_at`` is NEVER set on a live posting. It is stamped by ``close_posting``
only, i.e. after the posting has actually transitioned out of ``active``, and it
is UNSET again if the posting is later re-observed as live. ``set_purge_at``
raises rather than stamping an active doc. The matching index is deliberately
NOT a TTL index - see scripts/migrate_indexes.py for the reasoning.

Raw PyMongo only, no ORM. Every untrusted value passes InputSanitizer before it
reaches a Mongo filter (CLAUDE.md invariant 4), and every counter uses atomic
``$inc`` / ``find_one_and_update`` rather than read-modify-write, because
several Lambdas may run the same shard concurrently.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# --- versions ---------------------------------------------------------------
# Bumped when the normalizer below changes meaning. Stored on every doc so a
# re-normalization pass can find stale docs with an indexed equality filter.
NORMALIZATION_VERSION = 1
# The requirement extractor's contract version (ADR-002). A bump re-parses.
EXTRACTOR_VERSION = 1
# The fit-score engine version a stored score belongs to (ADR-002 makes this
# mandatory on every stored and served score).
SCORING_VERSION = 1

# --- vocabularies -----------------------------------------------------------
STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"
CLOSED_STATUSES = (STATUS_CLOSED, STATUS_EXPIRED)

# Closure reasons. Slug form, because `close_posting` runs them through
# `_safe_key` (which rewrites "_" to "-"); named here so callers and tests
# never have to guess which form landed in the document.
CLOSED_REASON_NOT_SEEN = "not-seen"      # the board stopped listing it
CLOSED_REASON_AGED_OUT = "aged-out"      # older than the servable window
CLOSED_REASON_UNVERIFIED = "unverified"  # no crawl has confirmed it in days

COST_CLASS_FREE = "free"    # direct ATS - guest-eligible (ADR-004)
COST_CLASS_PAID = "paid"    # metered aggregator - NEVER on the guest path
COST_CLASSES = (COST_CLASS_FREE, COST_CLASS_PAID)

WORK_MODE_REMOTE = "remote"
WORK_MODE_HYBRID = "hybrid"
WORK_MODE_ONSITE = "onsite"
WORK_MODE_UNKNOWN = "unknown"

# posted_at trust levels. ADR-001: an unknown date is NEVER replaced with now().
CONFIDENCE_EXACT = "exact"        # source published a real posting date
CONFIDENCE_RELATIVE = "relative"  # derived from "today" / "3 days ago"
CONFIDENCE_UNKNOWN = "unknown"    # no usable date - excluded from guest reads

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


# --- operational tunables ---------------------------------------------------
LEASE_TTL_SECONDS = 900           # 15 min: > the 110s fetch budget, < a schedule tick
# Retention. The guest contract is a 7-day window (ADR-001), so a posting older
# than that is not servable and only costs storage. Closed postings get a short
# grace so a board that flickers does not lose its rows, then go too.
MAX_INVENTORY_AGE_DAYS = _env_int("JOB_INVENTORY_MAX_AGE_DAYS", 7)
RETENTION_DAYS_AFTER_CLOSE = _env_int("JOB_INVENTORY_RETENTION_DAYS", 2)
RUN_RETENTION_DAYS = 30           # job_ingestion_runs log retention
# How much JD text stays in Mongo once the body is safely in S3. Zero by
# default: the scorer reads `requirements`, and tailoring resolves the body
# from object storage, so an inline copy buys nothing and costs a lot.
INVENTORY_DESCRIPTION_CHARS = _env_int("JOB_INVENTORY_DESCRIPTION_CHARS", 0)
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 6 * 3600

_MAX_DESCRIPTION_CHARS = 40000
_MAX_TITLE_CHARS = 300
_HEX_RE = re.compile(r"^[0-9a-f]{8,64}$")


# ---------------------------------------------------------------------------
# Collections (module scope client reuse via the DBConnect singleton)
# ---------------------------------------------------------------------------

def _db():
    return DBConnect().get_db()


def _postings():
    return _db().job_postings


def _source_state():
    return _db().job_source_state


def _runs():
    return _db().job_ingestion_runs


def _snapshots():
    return _db().job_match_snapshots


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Sanitization helpers - nothing untrusted reaches a filter unfiltered
# ---------------------------------------------------------------------------

def _safe_str(value: Any, max_length: int = 500) -> str:
    """Equality-position guard. Non-strings collapse to "" so a dict can never
    smuggle a Mongo operator into a filter, and $-prefixes are stripped."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return InputSanitizer.sanitize_mongo_query(str(value))[:max_length]


def _safe_key(value: Any, max_length: int = 200) -> str:
    """Slug form used for identity/lookup keys (source, employer_id, ...).
    Only [a-z0-9-] survives, so no key can carry a "." (dotted field path) or
    a "$" (operator position)."""
    raw = _safe_str(value, max_length).lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", raw)).strip("-")


def _safe_family(value: Any, max_length: int = 40) -> str:
    """Equality-position guard for taxonomy values (`role_family`, `seniority`).

    Separate from `_safe_key` because `_safe_key` rewrites "_" to "-", and the
    scorer's vocabulary is underscored ("software_engineering",
    "data_engineering", "customer_support"). Passing a family through
    `_safe_key` on ONE side of the query silently stored
    "software-engineering" while retrieval asked for the same string re-mangled
    from a different input, so multi-word families could never match. The
    injection posture is unchanged: "." and "$" still cannot survive, so no
    value here can reach an operator position or a dotted field path.
    """
    raw = _safe_str(value, max_length).lower()
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]+", "_", raw)).strip("_")


def _safe_display(value: Any, max_length: int = 200) -> str:
    """Guard for fields that are DISPLAYED, never filtered on.

    Critically NOT `_safe_str`. That routes through
    `InputSanitizer.sanitize_mongo_query`, which rewrites "." to "_" to stop
    dotted-field-path traversal. That is exactly right for a value heading into
    a query filter and actively destructive for content:

        "Acme, Inc."           -> "Acme, Inc_"
        "Sr. Software Engineer"-> "Sr_ Software Engineer"

    Control characters and length are still bounded, which is the whole threat
    surface for a value that only ever gets stored and rendered.
    """
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return InputSanitizer.sanitize_string(str(value), max_length)


def _safe_url(value: Any, max_length: int = 2000) -> str:
    """http(s) links, dots intact.

    The same "." to "_" rewrite turned every apply link into a dead host
    ("https://boards.greenhouse.io/..." -> "https://boards_greenhouse_io/..."),
    which would have shipped a broken Apply button on every single job card.
    Scheme is whitelisted here so a stored URL can never be javascript: or
    data:, mirroring `guest_service._safe_job_url` on the render side.
    """
    url = _safe_display(value, max_length).strip()
    if not url:
        return ""
    return url if url.lower().startswith(("http://", "https://")) else ""


def _safe_version(value: Any, max_length: int = 40) -> str:
    """Version strings keep their dots. Stored, compared for equality by the
    scorer, never placed in a Mongo filter - so the guard is a charset
    whitelist rather than the dotted-path stripper."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return re.sub(r"[^a-zA-Z0-9._\-]+", "", str(value))[:max_length]


def _safe_text(value: Any, max_length: int = _MAX_DESCRIPTION_CHARS) -> str:
    """Body text: stored, never used in an equality filter, so only length and
    control characters are bounded."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return InputSanitizer.sanitize_string(str(value), max_length)


def _safe_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _safe_skills(values: Any, limit: int = 60) -> List[str]:
    out: List[str] = []
    for v in (values or [])[:limit] if isinstance(values, (list, tuple)) else []:
        s = _safe_str(v, 80).lower().strip()
        if s and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_title(title: Any) -> str:
    """Lowercased, de-noised title used for grouping and equality lookups."""
    t = _safe_str(title, _MAX_TITLE_CHARS).lower()
    t = re.sub(r"\(.*?\)", " ", t)                  # trailing "(Remote)" etc.
    t = re.sub(r"[^a-z0-9+#/ .-]+", " ", t)
    t = re.sub(r"\b(job|req|requisition)?\s*id[:# ]*\w+\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def role_family_of(title: Any) -> str:
    """Role family, from the SAME taxonomy the scorer uses.

    This deliberately does NOT use `pipeline_intel_service._role_family`. That
    taxonomy has 13 families and is tech-only ("swe", "backend", "cloud_devops",
    else "other"), while `fit_score.ROLE_FAMILIES` has 31 and covers the whole
    labour market ("nursing", "teaching", "skilled_trades", "accounting", ...).

    Writing one vocabulary and reading with the other is not a style question,
    it is a silent zero-results bug in both directions:

      * the scorer classifies a resume as `software_engineering` while the
        inventory stamped the posting `swe`, so an equality filter matches
        NOTHING; and
      * every non-technical posting is stamped `other`, so nurses, teachers and
        accountants never see a single result no matter how well they match.

    `fit_score.classify_role` also refuses to guess: it returns None rather than
    defaulting an unrecognised title into an engineering family, which is why
    "other" here means genuinely unclassified and is never used to exclude a
    posting from retrieval.
    """
    try:
        from services import fit_score
        return fit_score.classify_role(normalize_title(title)) or "other"
    except Exception:  # pragma: no cover - taxonomy unavailable
        return "other"


# fit_score.title_level's vocabulary, plus the "no marker in the title" default.
SENIORITY_LEVELS = ("entry", "mid", "senior", "staff", "director", "executive")


def seniority_of(title: Any) -> str:
    """Seniority band, again from `fit_score` so writer and reader agree."""
    try:
        from services import fit_score
        return fit_score.title_level(normalize_title(title)) or "mid"
    except Exception:  # pragma: no cover
        return "mid"


def employer_id_of(name: Any) -> str:
    """Stable employer key. Legal suffixes are dropped so "Acme, Inc." and
    "Acme" collapse; this is identity for grouping, not a legal entity."""
    slug = _safe_key(name, 120)
    slug = re.sub(r"-(inc|llc|ltd|limited|corp|corporation|gmbh|plc|co)$", "", slug)
    return slug.strip("-")


def normalize_location(loc: Any) -> Dict[str, str]:
    """One location entry -> {raw, city, region, country, key}. `key` is the
    indexed equality form; `raw` preserves what the source actually said."""
    if isinstance(loc, dict):
        raw = _safe_display(loc.get("raw") or loc.get("name") or loc.get("text"), 200)
        city = _safe_display(loc.get("city"), 80)
        region = _safe_display(loc.get("region") or loc.get("state"), 80)
        country = _safe_display(loc.get("country"), 80)
    else:
        raw = _safe_display(loc, 200)
        city = region = country = ""
    if raw and not city:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts:
            city = _safe_display(parts[0], 80)
        if len(parts) > 1:
            region = region or _safe_display(parts[1], 80)
        if len(parts) > 2:
            country = country or _safe_display(parts[-1], 80)
    key = _safe_key("-".join(p for p in (city, region, country) if p), 160)
    return {"raw": raw, "city": city, "region": region, "country": country, "key": key}


def normalize_locations(values: Any, limit: int = 12) -> List[Dict[str, str]]:
    """Bounded on purpose: a posting listing 400 offices must not grow an
    unbounded array inside the doc."""
    items = values if isinstance(values, (list, tuple)) else ([values] if values else [])
    out: List[Dict[str, str]] = []
    seen = set()
    for item in list(items)[: limit * 3]:
        norm = normalize_location(item)
        if not norm["raw"]:
            continue
        if norm["key"] in seen:
            continue
        seen.add(norm["key"])
        out.append(norm)
        if len(out) >= limit:
            break
    return out


def normalize_work_mode(value: Any, *, title: Any = None, locations: Any = None) -> str:
    blob = " ".join(
        str(x or "") for x in (value, title, locations if isinstance(locations, str) else "")
    ).lower()
    if "hybrid" in blob:
        return WORK_MODE_HYBRID
    if "remote" in blob or "work from home" in blob or "wfh" in blob:
        return WORK_MODE_REMOTE
    if "on-site" in blob or "onsite" in blob or "in office" in blob or "in-office" in blob:
        return WORK_MODE_ONSITE
    return WORK_MODE_UNKNOWN


def compute_content_hash(
    *,
    employer_id: str,
    title_normalized: str,
    description_text: str,
    locations: Optional[List[Dict[str, str]]] = None,
    employment_type: str = "",
) -> str:
    """Digest of everything that changes what the posting MEANS.

    Deliberately EXCLUDES last_seen_at, posted_at and any counter, so
    re-observing an unchanged posting produces the same hash and does not
    trigger a re-parse. Whitespace is collapsed so cosmetic re-rendering by an
    ATS does not read as a content change."""
    loc_key = "|".join(sorted((l or {}).get("key", "") for l in (locations or [])))
    body = re.sub(r"\s+", " ", (description_text or "")).strip().lower()
    payload = "\x1f".join([
        (employer_id or "").strip().lower(),
        (title_normalized or "").strip().lower(),
        (employment_type or "").strip().lower(),
        loc_key,
        body,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_posting(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Fetcher payload -> canonical `job_postings` shape (no DB access).

    Every field is defaulted defensively: this collection has no schema
    enforcement, so the normalizer IS the schema.
    """
    raw = raw if isinstance(raw, dict) else {}
    source = _safe_key(raw.get("source"), 60)
    source_job_id = _safe_str(raw.get("source_job_id") or raw.get("job_id"), 200)
    employer_name = _safe_display(raw.get("employer_name") or raw.get("company"), 200)
    title_raw = _safe_display(raw.get("title") or raw.get("title_raw"), _MAX_TITLE_CHARS)
    title_normalized = normalize_title(title_raw)
    locations = normalize_locations(raw.get("locations") or raw.get("location"))
    description_text = _safe_text(raw.get("description_text") or raw.get("description"))
    employment_type = _safe_key(raw.get("employment_type"), 40)
    employer_id = _safe_key(raw.get("employer_id"), 120) or employer_id_of(employer_name)

    posted_at = _safe_dt(raw.get("posted_at"))
    confidence = _safe_key(raw.get("posted_at_confidence"), 20)
    if confidence not in (CONFIDENCE_EXACT, CONFIDENCE_RELATIVE, CONFIDENCE_UNKNOWN):
        confidence = CONFIDENCE_EXACT if posted_at else CONFIDENCE_UNKNOWN
    if posted_at is None:
        # ADR-001: never substitute now() for an unknown posting date.
        confidence = CONFIDENCE_UNKNOWN

    cost_class = _safe_key(raw.get("cost_class"), 10)
    if cost_class not in COST_CLASSES:
        cost_class = COST_CLASS_PAID  # fail CLOSED: unknown sources are not guest-eligible

    # The hash is ALWAYS over the full body, never the stored excerpt, so
    # offloading the text cannot change a posting's identity or spuriously
    # report a content change on the next crawl.
    content_hash = compute_content_hash(
        employer_id=employer_id,
        title_normalized=title_normalized,
        description_text=description_text,
        locations=locations,
        employment_type=employment_type,
    )

    doc = {
        "source": source,
        "source_job_id": source_job_id,
        "canonical_url": _safe_url(raw.get("canonical_url") or raw.get("url")),
        "employer_id": employer_id,
        "employer_name": employer_name,
        "title_raw": title_raw,
        "title_normalized": title_normalized,
        "role_family": _safe_family(raw.get("role_family")) or role_family_of(title_raw),
        "seniority": _safe_family(raw.get("seniority"), 20) or seniority_of(title_raw),
        "locations": locations,
        "work_mode": normalize_work_mode(
            raw.get("work_mode"), title=title_raw,
            locations=" ".join(l["raw"] for l in locations),
        ),
        "employment_type": employment_type,
        "posted_at": posted_at,
        "raw_posted_text": _safe_display(raw.get("raw_posted_text") or raw.get("posted_text"), 120),
        "posted_at_confidence": confidence,
        "source_quality": _safe_key(raw.get("source_quality"), 20) or "unknown",
        "cost_class": cost_class,
        # Registry provenance, denormalized onto the posting so the feed tabs
        # can filter by industry / ATS / company without re-joining the
        # catalogs. Slug-safe: all three land in equality filters.
        "industry": _safe_key(raw.get("industry"), 40) or "other",
        "ats": _safe_key(raw.get("ats"), 30),
        "tenant": _safe_key(raw.get("tenant"), 120),
        "normalization_version": NORMALIZATION_VERSION,
        "scoring_version": SCORING_VERSION,
        "content_hash": content_hash,
        "description_chars": len(description_text),
    }

    # Body offload. The matching path scores off `requirements`, never the raw
    # text, so a JD in Mongo is dead weight on a shared-tier cluster. Push it to
    # S3 under its content hash and keep only a pointer. If S3 is unavailable
    # (local dev, or a transient failure) we keep the body inline instead: a
    # bigger document is strictly better than a posting that can never be
    # tailored against.
    stored_body = description_text[:INVENTORY_DESCRIPTION_CHARS] \
        if INVENTORY_DESCRIPTION_CHARS > 0 else ""
    ref = None
    if description_text:
        try:
            from services import job_content_store
            ref = job_content_store.put_jd(content_hash, description_text)
        except Exception as exc:  # pragma: no cover - storage must not break ingest
            logger.warning("normalize_posting: JD offload failed (%s); keeping inline",
                           type(exc).__name__)
            ref = None
    doc["description_ref"] = ref
    doc["description_text"] = stored_body if ref else description_text
    return doc


# ---------------------------------------------------------------------------
# job_postings - idempotent write path
# ---------------------------------------------------------------------------

# Fields that identify the doc; never overwritten after insert.
_IDENTITY_FIELDS = ("source", "source_job_id")


def upsert_posting(
    raw: Dict[str, Any],
    *,
    run_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Idempotent upsert keyed on (source, source_job_id) - the ONE stable
    identity for a posting.

    At-least-once delivery is assumed (ADR-003: Lambda self-invocation), so
    delivering the same posting twice must be indistinguishable from delivering
    it once. It is: the second call finds the same content_hash, refreshes
    last_seen_at / last_verified_at, and reports ``content_changed=False`` so
    the caller does not re-parse.

    Returns ``{job_id, created, content_changed, needs_parse, content_hash,
    reopened}``. ``needs_parse`` is the ONLY signal a caller should use to
    decide whether to spend an LLM extraction on this JD.
    """
    now = now or _now()
    doc = normalize_posting(raw)
    if not doc["source"] or not doc["source_job_id"]:
        raise ValueError("upsert_posting requires both source and source_job_id")

    key = {"source": doc["source"], "source_job_id": doc["source_job_id"]}
    mutable = {k: v for k, v in doc.items() if k not in _IDENTITY_FIELDS}
    mutable.update({
        "status": STATUS_ACTIVE,
        "active": True,
        "last_seen_at": now,
        "last_verified_at": now,
        "updated_at": now,
    })
    if run_id:
        mutable["last_ingest_run_id"] = _safe_str(run_id, 64)

    update = {
        "$set": mutable,
        "$setOnInsert": {
            "job_id": uuid.uuid4().hex,
            "first_seen_at": now,
            "requirements": None,
            "closed_at": None,
            "closed_reason": None,
        },
        # A posting re-observed as live must NOT keep a retention stamp.
        # (purge_at is absent from $set, so there is no operator conflict.)
        "$unset": {"purge_at": ""},
    }

    try:
        before = _postings().find_one_and_update(
            key, update, upsert=True, return_document=ReturnDocument.BEFORE
        )
    except DuplicateKeyError:
        # Concurrent first-insert for the same key: the unique index did its
        # job. Retry once WITHOUT upsert - the doc now exists.
        before = _postings().find_one_and_update(
            key, update, return_document=ReturnDocument.BEFORE
        )

    created = before is None
    prev_hash = (before or {}).get("content_hash")
    content_changed = bool(created or prev_hash != doc["content_hash"])
    reopened = bool(before and (before.get("status") in CLOSED_STATUSES
                                or before.get("purge_at") is not None))

    if created:
        job_id = _postings().find_one(key, {"job_id": 1}).get("job_id")
    else:
        job_id = before.get("job_id")

    prev_req = (before or {}).get("requirements") or {}
    needs_parse = bool(
        content_changed
        or not prev_req
        or prev_req.get("source_content_hash") != doc["content_hash"]
        or prev_req.get("extractor_version") != EXTRACTOR_VERSION
    )

    return {
        "job_id": job_id,
        "created": created,
        "content_changed": content_changed,
        "needs_parse": needs_parse,
        "reopened": reopened,
        "content_hash": doc["content_hash"],
    }


def touch_seen(source: str, source_job_ids: Iterable[str],
               *, now: Optional[datetime] = None) -> int:
    """Cheap liveness refresh for postings re-observed in a listing page whose
    content we did not re-fetch. Bounded batch; no read-modify-write."""
    now = now or _now()
    ids = [_safe_str(i, 200) for i in list(source_job_ids or [])[:1000]]
    ids = [i for i in ids if i]
    if not ids:
        return 0
    res = _postings().update_many(
        {"source": _safe_key(source, 60), "source_job_id": {"$in": ids}},
        {"$set": {"last_seen_at": now, "last_verified_at": now, "updated_at": now}},
    )
    return int(res.modified_count or 0)


def mark_requirements_parsed(
    source: str,
    source_job_id: str,
    content_hash: str,
    requirements: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Store extracted requirements, guarded by the hash they were parsed FROM.

    ADR-002 makes the required-skill denominator come from the JD, so this is
    the write that makes fit scoring possible - and it must never regress. The
    content_hash sits IN THE FILTER: if the JD changed while the extraction was
    in flight, this update matches nothing and returns False rather than
    stamping stale requirements onto fresh text.

    The stored shape is `fit_score.extract_requirements`' OWN output, verbatim
    plus guard fields. That is deliberate: the read path feeds this straight
    back into `fit_score.score()`, so any reshaping here would be a second,
    drifting schema. An earlier revision persisted a four-field subset, which
    dropped `required_years`, `licenses`, `clearance_required`,
    `work_authorization_restricted`, `industries`, `work_mode` and `job_level` -
    every hard-constraint input the scorer has. A profile missing those does not
    score low, it scores HIGH (absent requirements earn neutral credit), so that
    failure would have surfaced as confidently wrong matches, not as an error.
    """
    now = now or _now()
    ch = _safe_str(content_hash, 64)
    if not _HEX_RE.match(ch):
        return False
    payload = requirements if isinstance(requirements, dict) else {}
    if not payload:
        return False

    licenses = payload.get("licenses")
    stored = {
        # --- fit_score-native profile (exactly what score() consumes) ------
        # NOT _safe_str: the mongo-query sanitizer strips "." (dotted field
        # paths), which rewrote "fit-v2.0" to "fit-v2_0". The scorer compares
        # this against FIT_SCORE_VERSION exactly, so the mangled value would
        # never match and every stored profile would be silently rejected -
        # correct results, but the JD-free fast path would never once engage.
        # This is a stored value that never reaches a filter, so a plain
        # charset whitelist is the right guard.
        "score_version": _safe_version(payload.get("score_version")),
        "requirements_source": _safe_key(payload.get("requirements_source"), 30),
        "required_skills": _safe_skills(payload.get("required_skills")),
        "preferred_skills": _safe_skills(payload.get("preferred_skills")),
        "required_years": payload.get("required_years"),
        "required_degree_level": int(payload.get("required_degree_level") or 0),
        "role_family": _safe_family(payload.get("role_family")) or None,
        "job_level": _safe_family(payload.get("job_level"), 20) or None,
        "industries": _safe_skills(payload.get("industries"), limit=20),
        "work_mode": _safe_key(payload.get("work_mode"), 20),
        "licenses": [
            {"key": _safe_key(l.get("key"), 40), "label": _safe_str(l.get("label"), 80)}
            for l in (licenses if isinstance(licenses, list) else [])[:10]
            if isinstance(l, dict)
        ],
        "clearance_required": bool(payload.get("clearance_required")),
        "work_authorization_restricted": bool(payload.get("work_authorization_restricted")),
        "text_length": int(payload.get("text_length") or 0),
        "content_hash": _safe_str(payload.get("content_hash"), 64),
        # --- guards (parse-once + never-regress) ---------------------------
        "source_content_hash": ch,
        "extractor_version": EXTRACTOR_VERSION,
        "extracted_at": now,
    }
    update: Dict[str, Any] = {"requirements": stored, "updated_at": now}

    # Role-family upgrade. `role_family` is stamped from the TITLE alone at
    # upsert time, and `classify_role` deliberately refuses to guess, so titles
    # like "Sr. Forward Deployed Engineer" or "Partner Engineer: Partner
    # Intelligence" land in "other" - unreachable by role-family retrieval even
    # though the body says plainly what the job is. The extractor re-runs
    # classification over the JD text, so when the title was unclassifiable and
    # the body is not, adopt the body's answer.
    #
    # Strictly an upgrade: a family derived from the title is never overwritten
    # by one derived from the body. A title is the employer's own summary and a
    # body can mention many roles ("you will partner with our nursing staff").
    body_family = _safe_family(payload.get("role_family"))
    if body_family:
        doc = _postings().find_one(
            {"source": _safe_key(source, 60),
             "source_job_id": _safe_str(source_job_id, 200)},
            {"_id": 0, "role_family": 1},
        )
        if doc and (doc.get("role_family") or "other") == "other":
            update["role_family"] = body_family

    res = _postings().update_one(
        {
            "source": _safe_key(source, 60),
            "source_job_id": _safe_str(source_job_id, 200),
            "content_hash": ch,
        },
        {"$set": update},
    )
    return bool(res.matched_count)


def needs_requirements_parse(doc: Dict[str, Any]) -> bool:
    """Read-path default for the parse-once rule. Safe on docs written before
    `requirements` existed (schema-on-read: absent means unparsed)."""
    if not isinstance(doc, dict):
        return False
    req = doc.get("requirements") or {}
    return (
        not req
        or req.get("source_content_hash") != doc.get("content_hash")
        or req.get("extractor_version") != EXTRACTOR_VERSION
    )


# ---------------------------------------------------------------------------
# job_postings - closure and retention
# ---------------------------------------------------------------------------

def close_posting(
    source: str,
    source_job_id: str,
    *,
    reason: str = "not_seen",
    status: str = STATUS_CLOSED,
    retention_days: int = RETENTION_DAYS_AFTER_CLOSE,
    now: Optional[datetime] = None,
) -> bool:
    """The ONLY path that stamps purge_at.

    Closure and the retention stamp happen in ONE update, so there is no window
    in which a doc is active-with-purge_at. The filter requires the doc to still
    be active, which makes a repeated close a no-op instead of pushing purge_at
    further out on every sweep.
    """
    now = now or _now()
    if status not in CLOSED_STATUSES:
        raise ValueError(f"close_posting status must be one of {CLOSED_STATUSES}")
    res = _postings().update_one(
        {
            "source": _safe_key(source, 60),
            "source_job_id": _safe_str(source_job_id, 200),
            "status": STATUS_ACTIVE,
        },
        {"$set": {
            "status": status,
            "active": False,
            "closed_at": now,
            "closed_reason": _safe_key(reason, 40),
            "purge_at": now + timedelta(days=max(0, int(retention_days))),
            "updated_at": now,
        }},
    )
    return bool(res.modified_count)


def close_unseen(
    source: str,
    *,
    seen_before: datetime,
    reason: str = "not_seen",
    limit: int = 5000,
    now: Optional[datetime] = None,
) -> int:
    """Close postings from ``source`` not re-observed since ``seen_before``.

    This is the closure half of a full-shard sweep: the run refreshes
    last_seen_at for everything it saw, then everything older is gone from the
    board. Bounded by ``limit`` so a single scheduled invocation stays inside
    the Lambda budget and is safely resumable on the next tick.
    """
    now = now or _now()
    cutoff = _safe_dt(seen_before)
    if cutoff is None:
        raise ValueError("close_unseen requires a timezone-aware seen_before")
    ids = [
        d["source_job_id"]
        for d in _postings().find(
            {"source": _safe_key(source, 60), "status": STATUS_ACTIVE,
             "last_seen_at": {"$lt": cutoff}},
            {"source_job_id": 1, "_id": 0},
        ).limit(max(1, int(limit)))
    ]
    closed = 0
    for sjid in ids:
        if close_posting(source, sjid, reason=reason, now=now):
            closed += 1
    return closed


def set_purge_at(source: str, source_job_id: str, when: datetime) -> bool:
    """Explicit retention stamp. Refuses to touch a live posting - the guard is
    the whole point of this function existing separately from close_posting."""
    doc = get_posting(source, source_job_id)
    if not doc:
        return False
    if doc.get("status") == STATUS_ACTIVE or doc.get("active"):
        raise ValueError(
            "refusing to set purge_at on an active posting "
            f"({doc.get('source')}/{doc.get('source_job_id')}); close it first"
        )
    res = _postings().update_one(
        {"source": _safe_key(source, 60), "source_job_id": _safe_str(source_job_id, 200),
         "status": {"$in": list(CLOSED_STATUSES)}},
        {"$set": {"purge_at": _safe_dt(when) or _now(), "updated_at": _now()}},
    )
    return bool(res.modified_count)


# ---------------------------------------------------------------------------
# Pinning - "a user is attached to this posting, it must never be lost"
# ---------------------------------------------------------------------------

PIN_REASONS = ("applied", "saved", "tailored", "snapshot", "manual")


def pin_posting(job_id: str, *, reason: str = "applied",
                now: Optional[datetime] = None) -> bool:
    """Mark a posting as user-attached AND archive it to S3 in the same call.

    Retention deletes anything past the 7-day window. That is correct for the
    other 99.9% of the inventory and catastrophic for the posting somebody
    applied to: the application row in `applications` would outlive the job it
    points at, and the user's own history would render as a dead reference.

    Pinning resolves that WITHOUT re-growing Mongo. The durable copy goes to S3
    (unbounded, cheap, exactly the right storage class for a cold immutable
    record); the Mongo row keeps a `pinned` flag so the purge path knows to
    verify the archive before deleting. After the purge the posting is still
    fully resolvable through `resolve_jd` - it just no longer occupies the hot
    collection.

    Idempotent: pinning twice re-archives (cheap, and it refreshes the snapshot
    if the posting changed) and adds the reason to the set.
    """
    now = now or _now()
    jid = _safe_str(job_id, 64)
    if not jid:
        return False
    doc = _postings().find_one({"job_id": jid})
    if not doc:
        # Already purged. If the archive exists the pin already did its job.
        try:
            from services import job_content_store
            return job_content_store.archive_exists(jid)
        except Exception:  # pragma: no cover
            return False

    archived = False
    try:
        from services import job_content_store
        archived = job_content_store.archive_posting(doc, reason=reason)
    except Exception as exc:  # pragma: no cover
        logger.error("pin_posting: archive raised for %s: %s", jid[:12], type(exc).__name__)

    safe_reason = _safe_key(reason, 20) or "manual"
    update: Dict[str, Any] = {
        "$set": {"pinned": True, "pinned_at": now, "updated_at": now},
        "$addToSet": {"pin_reasons": safe_reason},
    }
    if archived:
        update["$set"]["archived_at"] = now
    _postings().update_one({"job_id": jid}, update)

    if not archived:
        # The flag still lands, so the purge guard will refuse to delete this
        # row until a later sweep manages to archive it. Loud, because a
        # persistent failure here means retention is silently blocked.
        logger.error(
            "pin_posting: job %s pinned but NOT archived - it will be retained "
            "in Mongo until the archive succeeds", jid[:12]
        )
    return archived


def is_pinned(job_id: str) -> bool:
    doc = _postings().find_one({"job_id": _safe_str(job_id, 64)}, {"pinned": 1, "_id": 0})
    return bool((doc or {}).get("pinned"))


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def expire_aged_postings(
    *,
    max_age_days: Optional[int] = None,
    limit: int = 5000,
    now: Optional[datetime] = None,
) -> int:
    """Close every ACTIVE posting older than the servable window.

    Distinct from `close_unseen`, which handles "the board no longer lists it".
    This handles "it is still listed, but it is older than anything we will ever
    show". Both funnel into `close_posting`, so `purge_at` still has exactly one
    writer and the active-doc guard still holds.

    Retention days are 0 here: an aged-out posting has no audit value that the
    S3 archive does not already cover, and holding it for a grace period is what
    makes the collection grow without bound.
    """
    now = now or _now()
    limit_days = MAX_INVENTORY_AGE_DAYS if max_age_days is None else int(max_age_days)
    cutoff = now - timedelta(days=max(0, limit_days))
    rows = list(
        _postings()
        .find({"status": STATUS_ACTIVE, "posted_at": {"$lt": cutoff}},
              {"_id": 0, "source": 1, "source_job_id": 1})
        .limit(max(1, int(limit)))
    )
    closed = 0
    for row in rows:
        if close_posting(row["source"], row["source_job_id"],
                         reason=CLOSED_REASON_AGED_OUT,
                         retention_days=0, now=now):
            closed += 1
    return closed


def purge_expired(*, limit: int = 5000, now: Optional[datetime] = None
                  ) -> Dict[str, int]:
    """Retention sweeper. Deliberately explicit rather than a TTL index: a bug
    that stamped purge_at on a live posting would silently delete real
    inventory under a TTL, whereas this path is scheduled, bounded, logged, and
    double-checks the doc is not active.

    Pinned rows take the long way round. Before a pinned posting is deleted the
    S3 archive is VERIFIED to exist; if it does not, the row is left in place
    and re-archived on the next sweep. That check is the whole guarantee behind
    "a job you applied to is never lost", so it is a head request against object
    storage rather than trust in a flag.

    Returns counts rather than a bare int so the maintenance job can report
    what was retained and why.
    """
    now = now or _now()
    counts = {"deleted": 0, "archived": 0, "retained_unarchived": 0}

    candidates = list(
        _postings()
        .find({"purge_at": {"$lte": now}, "status": {"$in": list(CLOSED_STATUSES)}},
              {"_id": 0, "job_id": 1, "source": 1, "source_job_id": 1,
               "pinned": 1, "archived_at": 1})
        .limit(max(1, int(limit)))
    )
    if not candidates:
        return counts

    unpinned_ids = [d["job_id"] for d in candidates if not d.get("pinned") and d.get("job_id")]
    pinned = [d for d in candidates if d.get("pinned")]

    # Unpinned rows: nobody is attached, the 7-day window has passed, delete.
    for start in range(0, len(unpinned_ids), 500):
        res = _postings().delete_many({"job_id": {"$in": unpinned_ids[start:start + 500]}})
        counts["deleted"] += int(res.deleted_count or 0)

    if not pinned:
        return counts

    try:
        from services import job_content_store
    except Exception:  # pragma: no cover - without the store, retain everything
        counts["retained_unarchived"] += len(pinned)
        return counts

    for doc in pinned:
        jid = doc.get("job_id")
        if not jid:
            continue
        if not job_content_store.archive_exists(jid):
            full = _postings().find_one({"job_id": jid})
            if not (full and job_content_store.archive_posting(full, reason="retention")):
                counts["retained_unarchived"] += 1
                continue
            counts["archived"] += 1
        res = _postings().delete_one({"job_id": jid})
        counts["deleted"] += int(res.deleted_count or 0)

    if counts["retained_unarchived"]:
        logger.error(
            "purge_expired: %d pinned postings could not be archived and were "
            "RETAINED; inventory will not shrink until object storage recovers",
            counts["retained_unarchived"],
        )
    return counts


# ---------------------------------------------------------------------------
# job_postings - read paths
# ---------------------------------------------------------------------------

# Everything a job card and a fit score need, and nothing else. Notably ABSENT:
# `description_text`. Scoring reads the precomputed `requirements` profile
# (ADR-002 + `fit_score._precomputed_requirements`), so a 200-row candidate read
# moves ~200KB instead of ~800KB, and the JD body only leaves S3 when somebody
# actually tailors against the posting.
_CARD_PROJECTION = {
    "_id": 0, "job_id": 1, "source": 1, "source_job_id": 1, "canonical_url": 1,
    "employer_id": 1, "employer_name": 1, "title_raw": 1, "title_normalized": 1,
    "role_family": 1, "seniority": 1, "locations": 1, "work_mode": 1,
    "employment_type": 1, "posted_at": 1, "posted_at_confidence": 1,
    "cost_class": 1, "source_quality": 1, "content_hash": 1, "requirements": 1,
    "status": 1, "last_verified_at": 1, "description_ref": 1,
    "description_chars": 1, "industry": 1, "ats": 1, "tenant": 1,
}


def get_posting(source: str, source_job_id: str) -> Optional[Dict[str, Any]]:
    return _postings().find_one({
        "source": _safe_key(source, 60),
        "source_job_id": _safe_str(source_job_id, 200),
    })


def get_postings_map(source: str, source_job_ids: Iterable[str],
                     ) -> Dict[str, Dict[str, Any]]:
    """Bulk form of `get_posting`, keyed by source_job_id.

    The shard triage step ("has this posting changed since we last saw it?")
    runs once per listed row. Done one document at a time that is several
    hundred sequential round trips per shard, which on a 90s budget is the
    difference between a shard that ingests and a shard that times out doing
    bookkeeping. Batched in chunks so a large board cannot build an unbounded
    $in list.
    """
    src = _safe_key(source, 60)
    ids = [i for i in (_safe_str(v, 200) for v in (source_job_ids or [])) if i]
    out: Dict[str, Dict[str, Any]] = {}
    if not src or not ids:
        return out
    # Only the fields `_listing_implies_same_content` compares, so this stays a
    # small read even when a shard lists a few thousand postings.
    projection = {"_id": 0, "source_job_id": 1, "content_hash": 1, "status": 1,
                  "title_normalized": 1, "employment_type": 1, "locations": 1,
                  "description_chars": 1, "description_ref": 1,
                  "normalization_version": 1}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for doc in _postings().find({"source": src, "source_job_id": {"$in": chunk}},
                                    projection):
            out[doc["source_job_id"]] = doc
    return out


def get_by_job_id(job_id: str) -> Optional[Dict[str, Any]]:
    jid = _safe_str(job_id, 64)
    return _postings().find_one({"job_id": jid}) if jid else None


def find_by_content_hash(content_hash: str, *, limit: int = 25) -> List[Dict[str, Any]]:
    """All postings sharing a JD body - the cross-source dedup / parse-once
    lookup. Two boards carrying the same requisition hash identically, so the
    extraction is spent ONCE and reused."""
    ch = _safe_str(content_hash, 64)
    if not _HEX_RE.match(ch):
        return []
    return list(_postings().find({"content_hash": ch}, _CARD_PROJECTION).limit(int(limit)))


def find_parsed_requirements(content_hash: str) -> Optional[Dict[str, Any]]:
    """Requirements already extracted for this exact JD body, at the current
    extractor version. Returns None when a parse is genuinely needed."""
    ch = _safe_str(content_hash, 64)
    if not _HEX_RE.match(ch):
        return None
    doc = _postings().find_one(
        {"content_hash": ch, "normalization_version": NORMALIZATION_VERSION,
         "requirements.extractor_version": EXTRACTOR_VERSION,
         "requirements.source_content_hash": ch},
        {"_id": 0, "requirements": 1},
    )
    return (doc or {}).get("requirements")


def find_active_fresh(
    *,
    role_family: Optional[str] = None,
    role_families: Optional[Iterable[str]] = None,
    max_age_days: int = 7,
    cost_classes: Optional[Iterable[str]] = None,
    location_key: Optional[str] = None,
    work_mode: Optional[str] = None,
    seniority: Optional[str] = None,
    source: Optional[str] = None,
    industries: Optional[Iterable[str]] = None,
    tenants: Optional[Iterable[str]] = None,
    projection: Optional[Dict[str, Any]] = None,
    limit: int = 60,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """THE retrieval path: active + fresh, optionally by role family.

    Query shape (this is what the compound indexes in migrate_indexes.py are
    built from - equality, then sort, then range):

        {status: "active", role_family: <eq>, cost_class: {$in: [...]},
         posted_at: {$gte: cutoff}}  sort: {posted_at: -1}

    ADR-001: postings with an unknown date are excluded rather than assumed
    fresh - hence the explicit posted_at_confidence gate.
    ADR-004: callers on the guest path pass cost_classes=["free"], which is
    enforced structurally here, before any read, not by configuration.

    Normalized role/title/skill retrieval only. No Atlas Search, no vector
    index: nothing in this repo benchmarks a recall or latency win from one,
    and an unbenchmarked search index is a standing cost with an unproven
    benefit.
    """
    now = now or _now()
    cutoff = now - timedelta(days=max(0, int(max_age_days)))
    classes = [c for c in (cost_classes or COST_CLASSES) if c in COST_CLASSES]
    query: Dict[str, Any] = {
        "status": STATUS_ACTIVE,
        "cost_class": {"$in": classes} if len(classes) != 1 else classes[0],
        "posted_at": {"$gte": cutoff},
        "posted_at_confidence": {"$ne": CONFIDENCE_UNKNOWN},
    }
    # Role family accepts a SET, not just one value. A resume rarely maps to a
    # single family cleanly (a "Platform Engineer" is legitimately devops and
    # software_engineering; a "Data Engineer" overlaps data_engineering and
    # data_analysis), and an over-tight equality filter is indistinguishable
    # from an empty inventory from the user's side. The candidate set is widened
    # here and narrowed by the SCORER, which is the component that actually
    # knows how well the resume fits.
    families = [f for f in (
        _safe_family(f) for f in (list(role_families or []) + [role_family])
    ) if f]
    seen_f = []
    for f in families:
        if f not in seen_f:
            seen_f.append(f)
    if len(seen_f) == 1:
        query["role_family"] = seen_f[0]
    elif seen_f:
        query["role_family"] = {"$in": seen_f}
    lk = _safe_key(location_key, 160)
    if lk:
        query["locations.key"] = lk
    wm = _safe_key(work_mode, 20)
    if wm:
        query["work_mode"] = wm
    sen = _safe_family(seniority, 20)
    if sen:
        query["seniority"] = sen

    # Feed-tab filters. The authenticated tabs let a user narrow to a source, an
    # industry set or a specific company; those used to be applied by choosing
    # which catalog rows to crawl, which is meaningless once the crawl is
    # scheduled rather than per-request. They are read filters now.
    src = _safe_key(source, 60)
    if src:
        query["source"] = src
    inds = [i for i in (_safe_key(i, 40) for i in (industries or [])) if i]
    if inds:
        query["industry"] = {"$in": inds} if len(inds) > 1 else inds[0]
    tens = [t for t in (_safe_key(t, 120) for t in (tenants or [])) if t]
    if tens:
        query["tenant"] = {"$in": tens} if len(tens) > 1 else tens[0]

    return list(
        _postings()
        .find(query, projection or _CARD_PROJECTION)
        .sort("posted_at", -1)
        .limit(max(1, int(limit)))
    )


def find_stale_for_verification(
    source: str, *, older_than: datetime, limit: int = 200
) -> List[Dict[str, Any]]:
    """Maintenance path: active postings whose liveness has not been confirmed
    recently. Query: {status, source, last_verified_at: {$lt}} sort ascending -
    the oldest unverified first, so the sweep makes progress every tick."""
    cutoff = _safe_dt(older_than)
    if cutoff is None:
        raise ValueError("find_stale_for_verification requires a tz-aware datetime")
    return list(
        _postings()
        .find({"status": STATUS_ACTIVE, "source": _safe_key(source, 60),
               "last_verified_at": {"$lt": cutoff}},
              {"_id": 0, "job_id": 1, "source": 1, "source_job_id": 1,
               "canonical_url": 1, "last_verified_at": 1})
        .sort("last_verified_at", 1)
        .limit(max(1, int(limit)))
    )


def resolve_jd(job_id: str) -> Optional[Dict[str, Any]]:
    """Server-side JD resolution for tailoring. The JD text NEVER comes from
    the client (SSRF / injection posture, mirroring
    `guest_service.find_cached_match`) - it comes from inventory, fetched and
    parsed ONCE, keyed by content_hash.

    Three tiers, in order, because the body no longer lives in Mongo and the
    posting itself may have aged out of it:

      1. the live posting + its JD blob in S3 (the normal case);
      2. the S3 ARCHIVE, for a posting a user pinned that retention has since
         removed from Mongo - this is what makes "you applied to it, so it is
         still here" true after the 7-day window closes;
      3. nothing. A caller that gets None must not fall back to client-supplied
         JD text.
    """
    doc = get_by_job_id(job_id)

    try:
        from services import job_content_store
    except Exception:  # pragma: no cover
        job_content_store = None  # type: ignore

    if doc is None:
        if job_content_store is None:
            return None
        archived = job_content_store.get_archived(job_id)
        if not archived:
            return None
        return {
            "job_id": archived.get("job_id"),
            "title": archived.get("title_raw") or "",
            "company": archived.get("employer_name") or "",
            "jd_text": archived.get("description_text") or "",
            "url": archived.get("canonical_url") or "",
            "content_hash": archived.get("content_hash"),
            "requirements": archived.get("requirements"),
            "archived": True,
        }

    jd_text = doc.get("description_text") or ""
    ref = doc.get("description_ref")
    if not jd_text and ref and job_content_store is not None:
        jd_text = job_content_store.get_jd(ref) or ""
    return {
        "job_id": doc.get("job_id"),
        "title": doc.get("title_raw") or "",
        "company": doc.get("employer_name") or "",
        "jd_text": jd_text,
        "url": doc.get("canonical_url") or "",
        "content_hash": doc.get("content_hash"),
        "requirements": doc.get("requirements"),
        "archived": False,
    }


# ---------------------------------------------------------------------------
# job_source_state - leases, watermarks, backoff (the SQS replacement)
# ---------------------------------------------------------------------------

def _state_key(source: str, shard: str) -> Dict[str, str]:
    return {"source": _safe_key(source, 60), "shard": _safe_key(shard, 120) or "default"}


def ensure_source_state(source: str, shard: str,
                        *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Create the shard's state doc if absent. Idempotent: $setOnInsert only,
    so calling it on every tick never disturbs a live lease or watermark."""
    now = now or _now()
    key = _state_key(source, shard)
    _source_state().update_one(
        key,
        {"$setOnInsert": {
            **key,
            "watermark": None, "cursor": None, "etag": None, "checkpoint": None,
            "lease_owner": None, "lease_expiry": None, "lease_acquired_at": None,
            "last_success_at": None, "last_error": None, "last_error_at": None,
            "retry_count": 0, "next_attempt_at": None, "created_at": now,
        }},
        upsert=True,
    )
    return _source_state().find_one(key)


def acquire_lease(
    source: str,
    shard: str,
    owner: str,
    *,
    ttl_seconds: int = LEASE_TTL_SECONDS,
    respect_backoff: bool = True,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Bounded concurrency without a queue (ADR-003).

    One guarded ``find_one_and_update``: the doc is claimed only if the lease
    is free, already ours (re-entrant renewal), or EXPIRED - the expiry check
    lives in the filter, so a crashed worker's shard is stealable without any
    external reaper. Returns the state doc on success, None if another live
    owner holds it or the shard is still in backoff.

    Lease expiry is the visibility timeout SQS would have provided; because it
    can lapse mid-run, every write in the run must remain idempotent.
    """
    now = now or _now()
    owner_id = _safe_str(owner, 80)
    if not owner_id:
        raise ValueError("acquire_lease requires an owner id")
    key = _state_key(source, shard)
    ensure_source_state(key["source"], key["shard"], now=now)

    clauses: List[Dict[str, Any]] = [{
        "$or": [
            {"lease_owner": None},
            {"lease_owner": owner_id},
            {"lease_expiry": None},
            {"lease_expiry": {"$lte": now}},
        ]
    }]
    if respect_backoff:
        clauses.append({"$or": [{"next_attempt_at": None},
                                {"next_attempt_at": {"$lte": now}}]})

    return _source_state().find_one_and_update(
        {**key, "$and": clauses},
        {"$set": {
            "lease_owner": owner_id,
            "lease_expiry": now + timedelta(seconds=max(1, int(ttl_seconds))),
            "lease_acquired_at": now,
        }},
        return_document=ReturnDocument.AFTER,
    )


def renew_lease(source: str, shard: str, owner: str,
                *, ttl_seconds: int = LEASE_TTL_SECONDS,
                now: Optional[datetime] = None) -> bool:
    """Extend OUR lease only. A worker whose lease already lapsed and was
    stolen gets False and must stop writing shard-progress."""
    now = now or _now()
    res = _source_state().update_one(
        {**_state_key(source, shard), "lease_owner": _safe_str(owner, 80),
         "lease_expiry": {"$gt": now}},
        {"$set": {"lease_expiry": now + timedelta(seconds=max(1, int(ttl_seconds)))}},
    )
    return bool(res.modified_count)


def release_lease(source: str, shard: str, owner: str) -> bool:
    """Release guarded by ownership: a worker that lost its lease cannot free
    the shard out from under the worker that stole it."""
    res = _source_state().update_one(
        {**_state_key(source, shard), "lease_owner": _safe_str(owner, 80)},
        {"$set": {"lease_owner": None, "lease_expiry": None}},
    )
    return bool(res.modified_count)


def advance_watermark(
    source: str,
    shard: str,
    owner: str,
    watermark: datetime,
    *,
    cursor: Optional[str] = None,
    etag: Optional[str] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Monotonic watermark advance, guarded by lease ownership.

    The "$lt current" clause is IN THE FILTER, so an out-of-order or replayed
    update matches nothing and returns None rather than rewinding the shard and
    causing a re-scan of already-ingested pages. This is the ordering guarantee
    a FIFO queue would have supplied.
    """
    now = now or _now()
    wm = _safe_dt(watermark)
    if wm is None:
        raise ValueError("advance_watermark requires a timezone-aware datetime")
    updates: Dict[str, Any] = {"watermark": wm, "watermark_updated_at": now}
    if cursor is not None:
        updates["cursor"] = _safe_str(cursor, 500)
    if etag is not None:
        updates["etag"] = _safe_str(etag, 200)
    if isinstance(checkpoint, dict):
        updates["checkpoint"] = checkpoint
    return _source_state().find_one_and_update(
        {**_state_key(source, shard),
         "lease_owner": _safe_str(owner, 80),
         "$or": [{"watermark": None}, {"watermark": {"$lt": wm}}]},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )


def _backoff_seconds(retry_count: int) -> int:
    """Exponential backoff, capped. With no DLQ (ADR-003), backoff plus the
    `job_ingestion_runs` record IS the failure-isolation mechanism."""
    n = max(1, int(retry_count))
    return int(min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (n - 1))))


def record_failure(source: str, shard: str, error: Any,
                   *, owner: Optional[str] = None,
                   now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Atomic $inc of retry_count, then a backoff derived FROM the incremented
    value returned by the same operation - never a read-then-write."""
    now = now or _now()
    key = _state_key(source, shard)
    flt: Dict[str, Any] = dict(key)
    if owner:
        flt["lease_owner"] = _safe_str(owner, 80)
    doc = _source_state().find_one_and_update(
        flt,
        {"$inc": {"retry_count": 1},
         "$set": {"last_error": _safe_str(error, 500), "last_error_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        return None
    delay = _backoff_seconds(doc.get("retry_count") or 1)
    return _source_state().find_one_and_update(
        {**key, "retry_count": doc.get("retry_count")},
        {"$set": {"next_attempt_at": now + timedelta(seconds=delay),
                  "backoff_seconds": delay}},
        return_document=ReturnDocument.AFTER,
    ) or doc


def record_success(source: str, shard: str,
                   *, owner: Optional[str] = None,
                   now: Optional[datetime] = None) -> bool:
    now = now or _now()
    flt: Dict[str, Any] = dict(_state_key(source, shard))
    if owner:
        flt["lease_owner"] = _safe_str(owner, 80)
    res = _source_state().update_one(
        flt,
        {"$set": {"last_success_at": now, "last_error": None, "retry_count": 0,
                  "next_attempt_at": None, "backoff_seconds": 0}},
    )
    return bool(res.modified_count)


def get_source_state(source: str, shard: str) -> Optional[Dict[str, Any]]:
    return _source_state().find_one(_state_key(source, shard))


def due_shards(source: Optional[str] = None, *, limit: int = 50,
               now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Shards eligible to run: not leased (or lease expired) and out of
    backoff. This is the scheduler's pick list, replacing a queue receive."""
    now = now or _now()
    query: Dict[str, Any] = {
        "$and": [
            {"$or": [{"lease_owner": None}, {"lease_expiry": None},
                     {"lease_expiry": {"$lte": now}}]},
            {"$or": [{"next_attempt_at": None}, {"next_attempt_at": {"$lte": now}}]},
        ]
    }
    src = _safe_key(source, 60)
    if src:
        query["source"] = src
    return list(_source_state().find(query).limit(max(1, int(limit))))


# ---------------------------------------------------------------------------
# job_ingestion_runs - observability (the DLQ we do not have)
# ---------------------------------------------------------------------------

def start_run(source: str, shard: str, *, trace_id: Optional[str] = None,
              now: Optional[datetime] = None) -> str:
    now = now or _now()
    run_id = uuid.uuid4().hex
    key = _state_key(source, shard)
    _runs().insert_one({
        "run_id": run_id,
        **key,
        "status": "running",
        "trace_id": _safe_str(trace_id, 80) or run_id,
        "started_at": now,
        "finished_at": None,
        "duration_ms": None,
        "counts": {"fetched": 0, "upserted": 0, "created": 0, "changed": 0,
                   "unchanged": 0, "closed": 0, "parsed": 0, "errors": 0},
        "failures": [],
        "expire_at": now + timedelta(days=RUN_RETENTION_DAYS),
    })
    return run_id


def bump_run_counts(run_id: str, **counts: int) -> None:
    """Atomic $inc per counter. Several Lambdas may contribute to one run;
    read-modify-write here would silently lose increments."""
    rid = _safe_str(run_id, 64)
    inc = {f"counts.{_safe_key(k, 40)}": int(v)
           for k, v in (counts or {}).items() if isinstance(v, int)}
    if not rid or not inc:
        return
    _runs().update_one({"run_id": rid}, {"$inc": inc})


def record_run_failure(run_id: str, message: Any, *, item: Any = None) -> None:
    """Bounded failure list: $push with $slice keeps the doc from growing
    without limit when a shard fails thousands of items."""
    rid = _safe_str(run_id, 64)
    if not rid:
        return
    _runs().update_one(
        {"run_id": rid},
        {"$push": {"failures": {"$each": [{
            "message": _safe_str(message, 300),
            "item": _safe_str(item, 200),
            "at": _now(),
        }], "$slice": -50}},
         "$inc": {"counts.errors": 1}},
    )


def finish_run(run_id: str, *, status: str = "succeeded",
               now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    now = now or _now()
    rid = _safe_str(run_id, 64)
    doc = _runs().find_one({"run_id": rid}, {"started_at": 1})
    duration = None
    started = _safe_dt((doc or {}).get("started_at"))
    if started:
        duration = int((now - started).total_seconds() * 1000)
    return _runs().find_one_and_update(
        {"run_id": rid},
        {"$set": {"status": _safe_key(status, 20) or "succeeded",
                  "finished_at": now, "duration_ms": duration}},
        return_document=ReturnDocument.AFTER,
    )


def recent_runs(source: Optional[str] = None, *, limit: int = 20) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {}
    src = _safe_key(source, 60)
    if src:
        query["source"] = src
    return list(_runs().find(query, {"_id": 0}).sort("started_at", -1).limit(int(limit)))


# ---------------------------------------------------------------------------
# job_match_snapshots bridge (ADDITIVE ONLY - the existing path keeps working)
# ---------------------------------------------------------------------------

def link_snapshot_jobs(snapshot_id: str, match_to_job: Dict[str, str]) -> int:
    """Attach canonical job ids to an existing snapshot.

    The existing snapshot doc stores full JD text per guest per day under
    `jd_sources` (guest_service:1794-1809, capped at GUEST_JD_SOURCES_MAX after
    the Atlas 512MB incident). That path is LEFT WORKING. This adds
    `job_refs: {match_id -> job_id}` and a flat `job_ids` array beside it, so
    new readers can resolve the JD from inventory - fetched and parsed once -
    instead of from a per-guest copy.

    match_ids are validated as hex before being used as a dotted field path: a
    key containing "." or "$" would otherwise write into an unintended path.
    """
    sid = _safe_str(snapshot_id, 64)
    if not sid or not isinstance(match_to_job, dict):
        return 0
    sets: Dict[str, str] = {}
    job_ids: List[str] = []
    for match_id, job_id in list(match_to_job.items())[:200]:
        mid = _safe_str(match_id, 64)
        jid = _safe_str(job_id, 64)
        if not _HEX_RE.match(mid) or not _HEX_RE.match(jid):
            continue
        sets[f"job_refs.{mid}"] = jid
        job_ids.append(jid)
    if not sets:
        return 0
    res = _snapshots().update_one(
        {"snapshot_id": sid},
        {"$set": {**sets, "inventory_linked_at": _now()},
         "$addToSet": {"job_ids": {"$each": job_ids}}},
    )
    return len(sets) if res.matched_count else 0


def snapshot_job_ref(snapshot_id: str, match_id: str) -> Optional[str]:
    """Resolve a served match_id to its canonical job_id, or None when the
    snapshot predates inventory linking (read-path default: callers fall back
    to the existing jd_sources path)."""
    sid = _safe_str(snapshot_id, 64)
    mid = _safe_str(match_id, 64)
    if not sid or not _HEX_RE.match(mid):
        return None
    doc = _snapshots().find_one({"snapshot_id": sid}, {"_id": 0, "job_refs": 1})
    return ((doc or {}).get("job_refs") or {}).get(mid)
