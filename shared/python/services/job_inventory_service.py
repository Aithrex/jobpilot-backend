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

# --- operational tunables ---------------------------------------------------
LEASE_TTL_SECONDS = 900           # 15 min: > the 110s fetch budget, < a schedule tick
RETENTION_DAYS_AFTER_CLOSE = 30   # how long a closed posting is kept for audit
RUN_RETENTION_DAYS = 30           # job_ingestion_runs log retention
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
    """Reuse the ONE existing role taxonomy rather than inventing a second one
    (`pipeline_intel_service._role_family`). Imported lazily to keep this
    module's cold-start import graph flat."""
    try:
        from services.pipeline_intel_service import _role_family
        return _role_family(normalize_title(title))
    except Exception:  # pragma: no cover - taxonomy unavailable
        return "other"


def seniority_of(title: Any) -> str:
    try:
        from services.pipeline_intel_service import _seniority
        return _seniority(normalize_title(title))
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
        raw = _safe_str(loc.get("raw") or loc.get("name") or loc.get("text"), 200)
        city = _safe_str(loc.get("city"), 80)
        region = _safe_str(loc.get("region") or loc.get("state"), 80)
        country = _safe_str(loc.get("country"), 80)
    else:
        raw = _safe_str(loc, 200)
        city = region = country = ""
    if raw and not city:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts:
            city = _safe_str(parts[0], 80)
        if len(parts) > 1:
            region = region or _safe_str(parts[1], 80)
        if len(parts) > 2:
            country = country or _safe_str(parts[-1], 80)
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
    employer_name = _safe_str(raw.get("employer_name") or raw.get("company"), 200)
    title_raw = _safe_str(raw.get("title") or raw.get("title_raw"), _MAX_TITLE_CHARS)
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

    doc = {
        "source": source,
        "source_job_id": source_job_id,
        "canonical_url": _safe_str(raw.get("canonical_url") or raw.get("url"), 2000),
        "employer_id": employer_id,
        "employer_name": employer_name,
        "title_raw": title_raw,
        "title_normalized": title_normalized,
        "role_family": _safe_key(raw.get("role_family"), 40) or role_family_of(title_raw),
        "seniority": _safe_key(raw.get("seniority"), 20) or seniority_of(title_raw),
        "locations": locations,
        "work_mode": normalize_work_mode(
            raw.get("work_mode"), title=title_raw,
            locations=" ".join(l["raw"] for l in locations),
        ),
        "employment_type": employment_type,
        "description_text": description_text,
        "posted_at": posted_at,
        "raw_posted_text": _safe_str(raw.get("raw_posted_text") or raw.get("posted_text"), 120),
        "posted_at_confidence": confidence,
        "source_quality": _safe_key(raw.get("source_quality"), 20) or "unknown",
        "cost_class": cost_class,
        "normalization_version": NORMALIZATION_VERSION,
        "scoring_version": SCORING_VERSION,
    }
    doc["content_hash"] = compute_content_hash(
        employer_id=employer_id,
        title_normalized=title_normalized,
        description_text=description_text,
        locations=locations,
        employment_type=employment_type,
    )
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
    """
    now = now or _now()
    ch = _safe_str(content_hash, 64)
    if not _HEX_RE.match(ch):
        return False
    payload = requirements if isinstance(requirements, dict) else {}
    res = _postings().update_one(
        {
            "source": _safe_key(source, 60),
            "source_job_id": _safe_str(source_job_id, 200),
            "content_hash": ch,
        },
        {"$set": {
            "requirements": {
                "required_skills": _safe_skills(payload.get("required_skills")),
                "preferred_skills": _safe_skills(payload.get("preferred_skills")),
                "min_years_experience": payload.get("min_years_experience"),
                "education": _safe_str(payload.get("education"), 200),
                "constraints": {
                    "work_authorization": _safe_str(
                        (payload.get("constraints") or {}).get("work_authorization"), 200),
                    "clearance": _safe_str(
                        (payload.get("constraints") or {}).get("clearance"), 200),
                    "licenses": _safe_skills((payload.get("constraints") or {}).get("licenses")),
                },
                "source_content_hash": ch,
                "extractor_version": EXTRACTOR_VERSION,
                "extracted_at": now,
            },
            "updated_at": now,
        }},
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


def purge_expired(*, limit: int = 5000, now: Optional[datetime] = None) -> int:
    """Retention sweeper. Deliberately explicit rather than a TTL index: a bug
    that stamped purge_at on a live posting would silently delete real
    inventory under a TTL, whereas this path is scheduled, bounded, logged, and
    double-checks the doc is not active."""
    now = now or _now()
    res = _postings().delete_many({
        "purge_at": {"$lte": now},
        "status": {"$in": list(CLOSED_STATUSES)},
    })
    return int(res.deleted_count or 0)


# ---------------------------------------------------------------------------
# job_postings - read paths
# ---------------------------------------------------------------------------

_CARD_PROJECTION = {
    "_id": 0, "job_id": 1, "source": 1, "source_job_id": 1, "canonical_url": 1,
    "employer_id": 1, "employer_name": 1, "title_raw": 1, "title_normalized": 1,
    "role_family": 1, "seniority": 1, "locations": 1, "work_mode": 1,
    "employment_type": 1, "posted_at": 1, "posted_at_confidence": 1,
    "cost_class": 1, "source_quality": 1, "content_hash": 1, "requirements": 1,
    "status": 1, "last_verified_at": 1,
}


def get_posting(source: str, source_job_id: str) -> Optional[Dict[str, Any]]:
    return _postings().find_one({
        "source": _safe_key(source, 60),
        "source_job_id": _safe_str(source_job_id, 200),
    })


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
    max_age_days: int = 7,
    cost_classes: Optional[Iterable[str]] = None,
    location_key: Optional[str] = None,
    work_mode: Optional[str] = None,
    seniority: Optional[str] = None,
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
    rf = _safe_key(role_family, 40)
    if rf:
        query["role_family"] = rf
    lk = _safe_key(location_key, 160)
    if lk:
        query["locations.key"] = lk
    wm = _safe_key(work_mode, 20)
    if wm:
        query["work_mode"] = wm
    sen = _safe_key(seniority, 20)
    if sen:
        query["seniority"] = sen
    return list(
        _postings()
        .find(query, _CARD_PROJECTION)
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
    parsed ONCE, keyed by content_hash."""
    doc = get_by_job_id(job_id)
    if not doc:
        return None
    return {
        "job_id": doc.get("job_id"),
        "title": doc.get("title_raw") or "",
        "company": doc.get("employer_name") or "",
        "jd_text": doc.get("description_text") or "",
        "url": doc.get("canonical_url") or "",
        "content_hash": doc.get("content_hash"),
        "requirements": doc.get("requirements"),
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
