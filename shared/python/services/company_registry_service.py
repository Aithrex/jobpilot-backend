"""Company registry — the employer universe, in MongoDB rather than in Python.

Why this exists
---------------
The crawlable universe today is three Python literals: 1013 Workday tenants,
1319 career-page companies and 11 enterprise rows. That is ~2,340 employers
against a US market of roughly 11 million employer establishments, and every
addition costs a human a catalog row. A registry that must be typed cannot grow
to the size of the problem.

This module owns three collections:

``companies``
    One row per employer identity, from state business registries and federal
    signal sources. Millions of rows, write-light, read by priority.

``company_ats_endpoints``
    One row per crawlable board. This is the runtime twin of a catalog row, and
    it is what `job_ingest_worker` shards over. An endpoint is discovered as
    ``candidate``, promoted to ``validated`` when it actually serves postings,
    and only ever demoted - never deleted on a single failure.

``discovery_queue``
    Priority-ordered work. Uses the ADR-003 lease pattern so a shard can claim
    items without a second locking mechanism.

The catalogs keep working
-------------------------
`seed_from_catalogs()` imports the existing Python lists as ``validated``
endpoints, and `iter_endpoints()` returns the UNION of catalog rows and Mongo
rows. So nothing regresses while Mongo is empty, and the migration is additive
rather than a cutover. `job_ingest_worker` keeps reading its catalogs directly
if this collection is unavailable.

Prioritisation is the whole game
--------------------------------
State registries return every LLC ever filed - tens of millions of rows, the
overwhelming majority single-member holding companies with no employees and no
careers page. Probing them all is neither possible nor useful, so `priority`
(§4.5 of ADR-004) is what makes the tail tractable: companies with a real
employment signal (H-1B filings, federal registration, SEC filer status) are
probed first, and the unscored tail drains forever at low priority. The system
is designed to run indefinitely, not to finish.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

COMPANIES = "companies"
ENDPOINTS = "company_ats_endpoints"
QUEUE = "discovery_queue"

# --- endpoint lifecycle (mirrors ats_providers.base) ------------------------
STATUS_CANDIDATE = "candidate"
STATUS_VALIDATED = "validated"
STATUS_EMPTY = "empty"
STATUS_DEAD = "dead"
STATUS_BLOCKED = "blocked"

# --- discovery lifecycle ----------------------------------------------------
DISCOVERY_PENDING = "pending"
DISCOVERY_RUNNING = "running"
DISCOVERY_DONE = "done"
DISCOVERY_NO_PORTAL = "no_portal"
DISCOVERY_FAILED = "failed"

#: How many consecutive validation failures retire an endpoint. Deliberately
#: high: employers freeze hiring, ATS hosts have outages, and rediscovering a
#: board costs far more than carrying a quiet one.
DEAD_AFTER_FAILURES = 8

#: Endpoints are re-validated on this cadence so ATS migrations are noticed.
#: Catalog decay is the failure mode this prevents: an employer moves from
#: Greenhouse to Workday and the row silently returns zero jobs forever.
REVALIDATE_AFTER_DAYS = 21

LEASE_TTL_SECONDS = 900


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _col(name: str):
    return _db()[name]


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

#: Legal suffixes stripped before matching. Order matters only in that the regex
#: is anchored at the end and applied repeatedly ("ACME HOLDINGS LLC INC").
_SUFFIXES = (
    "incorporated", "corporation", "company", "limited", "holdings", "holding",
    "llc", "l l c", "inc", "corp", "co", "ltd", "lp", "llp", "lllp", "plc",
    "pllc", "pc", "pa", "na", "sa", "ag", "gmbh", "bv", "nv", "ab", "oy", "as",
    "the",
)
_SUFFIX_RE = re.compile(
    r"[\s,.]*\b(?:%s)\b[\s,.]*$" % "|".join(re.escape(s) for s in _SUFFIXES),
    re.IGNORECASE)
#: A leading article is not a suffix, so the end-anchored pattern above cannot
#: reach it — and "The Acme Widgets LLC" must join with "Acme Widgets Inc" or the
#: two sources never merge.
_LEADING_ARTICLE_RE = re.compile(r"^(?:the)\s+", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]+")
_WS_RE = re.compile(r"\s+")


def normalize_company_name(name: Any) -> str:
    """Deterministic join key across registries, EDGAR, DOL and SAM.

    The same employer appears as "Acme Widgets, LLC", "ACME WIDGETS L.L.C." and
    "Acme Widgets Inc" across four sources, so matching has to happen on a
    canonical form. Suffixes are stripped repeatedly because compound tails
    ("... Holdings LLC") are common.

    Returns "" for input that normalises away entirely, which the caller must
    treat as "unusable identity" rather than as a valid empty key.
    """
    text = _PUNCT_RE.sub(" ", str(name or "")).strip().lower()
    text = _WS_RE.sub(" ", text)
    if not text:
        return ""
    prev = None
    while prev != text:
        prev = text
        text = _SUFFIX_RE.sub("", text).strip()
        text = _LEADING_ARTICLE_RE.sub("", text).strip()
    return _WS_RE.sub(" ", text).strip().upper()


def company_id_for(name: Any, state: Any = "") -> str:
    """Stable id. Name+state rather than name alone: "Acme Consulting" in Ohio
    and in Oregon are different employers, and collapsing them would merge two
    unrelated hiring pipelines."""
    key = f"{normalize_company_name(name)}|{str(state or '').strip().upper()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Priority scoring
# ---------------------------------------------------------------------------

#: Weights are ordered by how strongly a signal predicts "this employer has a
#: public careers page worth crawling". H-1B is top because an LCA filing is
#: documentary proof of hiring, and of the sponsorship our users filter on.
_SIGNAL_WEIGHTS: Dict[str, int] = {
    "h1b": 50,
    "edgar": 30,
    "sam": 20,
    "known_domain": 15,
    "multi_state": 10,
    "recent_filing": 5,
}


def score_priority(signals: Optional[Dict[str, Any]] = None,
                   *, employees: Optional[int] = None) -> int:
    """0-100. Higher is probed sooner.

    Never returns 0 for a real company: the unscored tail must still drain, just
    slowly. A floor of 1 keeps every row eligible rather than creating a class
    of entities that can never be reached.
    """
    total = 0
    for key, weight in _SIGNAL_WEIGHTS.items():
        if (signals or {}).get(key):
            total += weight
    if employees:
        try:
            n = int(employees)
        except (TypeError, ValueError):
            n = 0
        if n >= 10000:
            total += 25
        elif n >= 1000:
            total += 18
        elif n >= 100:
            total += 10
        elif n >= 10:
            total += 4
    return max(1, min(100, total))


# ---------------------------------------------------------------------------
# companies
# ---------------------------------------------------------------------------

def upsert_company(record: Dict[str, Any]) -> str:
    """Insert or merge one employer. Returns its company_id.

    Merge rather than replace: the same employer arrives from several sources
    and each contributes different fields (a state registry has the address, DOL
    has the H-1B signal, EDGAR has the ticker). Overwriting would make the last
    writer win and silently discard the others.
    """
    name = str(record.get("name") or "").strip()
    if not name:
        raise ValueError("company record requires a name")
    state = str(record.get("state") or "").strip().upper()[:2]
    cid = record.get("company_id") or company_id_for(name, state)

    signals = dict(record.get("signals") or {})
    now = _now()
    set_fields: Dict[str, Any] = {
        "name": name,
        "name_normalized": normalize_company_name(name),
        "state": state,
        "updated_at": now,
    }
    for key in ("domain", "entity_type", "status", "registered_at",
                "address", "agent_name", "source_url", "ticker", "cik",
                "naics", "sic"):
        val = record.get(key)
        if val not in (None, "", {}, []):
            set_fields[key] = val

    # $addToSet on jurisdictions: an employer registered in twelve states is one
    # employer with twelve registrations, not twelve companies.
    add_to_set: Dict[str, Any] = {}
    juris = record.get("jurisdiction") or state
    if juris:
        add_to_set["jurisdictions"] = str(juris).strip().upper()[:2]
    if record.get("sources"):
        add_to_set.setdefault("sources", {"$each": list(record["sources"])})
    elif record.get("source"):
        add_to_set["sources"] = str(record["source"])

    update: Dict[str, Any] = {
        "$set": set_fields,
        "$setOnInsert": {
            "company_id": cid,
            "created_at": now,
            "discovery_status": DISCOVERY_PENDING,
        },
    }
    if add_to_set:
        update["$addToSet"] = add_to_set
    if signals:
        for key, val in signals.items():
            update["$set"][f"signals.{key}"] = val

    col = _col(COMPANIES)
    col.update_one({"company_id": cid}, update, upsert=True)

    # Priority depends on the MERGED signal set, so it is recomputed from the
    # stored doc rather than from this one caller's partial view.
    doc = col.find_one({"company_id": cid},
                       {"signals": 1, "jurisdictions": 1, "domain": 1})
    merged = dict((doc or {}).get("signals") or {})
    if (doc or {}).get("domain"):
        merged["known_domain"] = True
    if len((doc or {}).get("jurisdictions") or []) > 1:
        merged["multi_state"] = True
    col.update_one({"company_id": cid},
                   {"$set": {"priority": score_priority(merged)}})
    return cid


def bulk_upsert_companies(records: Iterable[Dict[str, Any]],
                          *, batch_size: int = 1000) -> Dict[str, int]:
    """Batched ingest for registry pulls.

    Uses `UpdateOne(upsert=True)` in unordered bulk writes: at millions of rows
    per state the per-document round trip in `upsert_company` is the bottleneck,
    and unordered means one malformed row cannot abort the batch. Priority is
    computed inline from the record's own signals here (rather than re-read per
    doc) - the periodic rescore in `refresh_priorities` reconciles merges.
    """
    from pymongo import UpdateOne

    col = _col(COMPANIES)
    ops: List[Any] = []
    stats = {"seen": 0, "written": 0, "skipped": 0}
    now = _now()

    def _flush() -> None:
        if not ops:
            return
        try:
            res = col.bulk_write(ops, ordered=False)
            stats["written"] += (res.upserted_count or 0) + (res.modified_count or 0)
        except Exception as exc:
            logger.warning("company bulk_write failed: %s", type(exc).__name__)
        ops.clear()

    for rec in records:
        stats["seen"] += 1
        if hasattr(rec, "as_upsert"):
            rec = rec.as_upsert()
        name = str(rec.get("name") or "").strip()
        if not rec.get("registered_at") and rec.get("registered_on"):
            rec = dict(rec)
            rec["registered_at"] = rec["registered_on"]
        norm = normalize_company_name(name)
        if not name or not norm:
            stats["skipped"] += 1
            continue
        state = str(rec.get("state") or "").strip().upper()[:2]
        cid = rec.get("company_id") or company_id_for(name, state)
        signals = dict(rec.get("signals") or {})

        set_fields: Dict[str, Any] = {"name": name, "name_normalized": norm,
                                      "state": state, "updated_at": now}
        for key in ("domain", "entity_type", "status", "registered_at",
                    "address", "agent_name", "source_url", "naics",
                    "ticker", "cik"):
            val = rec.get(key)
            if val not in (None, "", {}, []):
                set_fields[key] = val
        for key, val in signals.items():
            set_fields[f"signals.{key}"] = val

        update: Dict[str, Any] = {
            "$set": set_fields,
            "$setOnInsert": {"company_id": cid, "created_at": now,
                             "discovery_status": DISCOVERY_PENDING,
                             "priority": score_priority(signals)},
        }
        add: Dict[str, Any] = {}
        if state:
            add["jurisdictions"] = state
        if rec.get("source"):
            add["sources"] = str(rec["source"])
        if add:
            update["$addToSet"] = add

        ops.append(UpdateOne({"company_id": cid}, update, upsert=True))
        if len(ops) >= max(1, int(batch_size)):
            _flush()
    _flush()
    return stats


def get_company(company_id: str) -> Optional[Dict[str, Any]]:
    return _col(COMPANIES).find_one({"company_id": company_id})


def find_company_by_name(name: str, state: str = "") -> Optional[Dict[str, Any]]:
    return _col(COMPANIES).find_one({"company_id": company_id_for(name, state)})


def company_counts() -> Dict[str, Any]:
    col = _col(COMPANIES)
    by_state = list(col.aggregate([
        {"$group": {"_id": "$state", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]))
    return {
        "total": col.estimated_document_count(),
        "with_domain": col.count_documents({"domain": {"$nin": [None, ""]}}),
        "by_state": {str(r["_id"] or "??"): r["n"] for r in by_state},
    }


# ---------------------------------------------------------------------------
# company_ats_endpoints
# ---------------------------------------------------------------------------

def upsert_endpoint(endpoint: Any, *, company_id: str = "",
                    display_name: str = "", industry: str = "",
                    source: str = "discovery") -> str:
    """Record a crawlable board. Returns its member_key.

    `member_key` is the identity, not (vendor, tenant): under ADR-003 it is the
    prefix of every `source_job_id` this board writes, so it must be stable and
    unique. Two sites on one Oracle pod are two boards and get two keys.
    """
    doc = endpoint.to_doc() if hasattr(endpoint, "to_doc") else dict(endpoint)
    key = doc.get("member_key")
    if not key:
        raise ValueError("endpoint has no member_key")

    now = _now()
    set_fields = {
        "vendor": doc.get("vendor"), "tenant": doc.get("tenant"),
        "site": doc.get("site") or "", "host": doc.get("host") or "",
        "extra": doc.get("extra") or {}, "updated_at": now,
    }
    if company_id:
        set_fields["company_id"] = company_id
    if display_name:
        set_fields["display_name"] = display_name
    if industry:
        set_fields["industry"] = industry

    _col(ENDPOINTS).update_one(
        {"member_key": key},
        {"$set": set_fields,
         "$setOnInsert": {
             "member_key": key, "created_at": now,
             "status": doc.get("status") or STATUS_CANDIDATE,
             "consecutive_failures": 0, "job_count_last": 0,
             "validated_at": None, "last_seen_at": None,
         },
         "$addToSet": {"sources": source}},
        upsert=True)
    return key


def record_validation(member_key: str, result: Any) -> None:
    """Fold a ValidationResult into an endpoint's lifecycle.

    The three-way distinction is the point. EMPTY (reachable, nothing open) and
    BLOCKED (anti-bot wall) must NOT accumulate toward death: a hiring freeze and
    a rate limit are both temporary, and retiring on either would mean paying
    discovery cost again later. Only hard failures count, and only
    DEAD_AFTER_FAILURES of them in a row.
    """
    status = getattr(result, "status", None) or str(result)
    count = int(getattr(result, "job_count", 0) or 0)
    detail = str(getattr(result, "detail", "") or "")[:300]
    now = _now()

    set_fields: Dict[str, Any] = {"last_checked_at": now,
                                  "last_check_detail": detail,
                                  "last_check_status": status}
    inc: Dict[str, int] = {}

    if status == STATUS_VALIDATED:
        set_fields.update({"status": STATUS_VALIDATED, "validated_at": now,
                           "last_seen_at": now, "job_count_last": count,
                           "consecutive_failures": 0})
    elif status == STATUS_EMPTY:
        set_fields.update({"status": STATUS_EMPTY, "last_seen_at": now,
                           "job_count_last": 0, "consecutive_failures": 0})
    elif status == STATUS_BLOCKED:
        # Reachability unknown. Leave the previous status alone so a validated
        # board stays validated through a throttling episode.
        set_fields["last_blocked_at"] = now
    else:
        inc["consecutive_failures"] = 1

    update: Dict[str, Any] = {"$set": set_fields}
    if inc:
        update["$inc"] = inc
    col = _col(ENDPOINTS)
    col.update_one({"member_key": member_key}, update)

    if inc:
        col.update_one(
            {"member_key": member_key,
             "consecutive_failures": {"$gte": DEAD_AFTER_FAILURES}},
            {"$set": {"status": STATUS_DEAD, "died_at": now}})


def iter_endpoints(*, vendor: Optional[str] = None,
                   statuses: Sequence[str] = (STATUS_VALIDATED, STATUS_EMPTY),
                   limit: int = 0) -> Iterator[Dict[str, Any]]:
    query: Dict[str, Any] = {"status": {"$in": list(statuses)}}
    if vendor:
        query["vendor"] = vendor
    cur = _col(ENDPOINTS).find(query).sort("member_key", 1)
    if limit:
        cur = cur.limit(int(limit))
    for doc in cur:
        yield doc


def endpoints_needing_revalidation(limit: int = 500) -> List[Dict[str, Any]]:
    cutoff = _now() - timedelta(days=REVALIDATE_AFTER_DAYS)
    return list(_col(ENDPOINTS).find({
        "status": {"$in": [STATUS_VALIDATED, STATUS_EMPTY, STATUS_BLOCKED]},
        "$or": [{"last_checked_at": {"$lt": cutoff}},
                {"last_checked_at": None}],
    }).limit(int(limit)))


def endpoint_counts() -> Dict[str, Any]:
    col = _col(ENDPOINTS)
    rows = list(col.aggregate([
        {"$group": {"_id": {"vendor": "$vendor", "status": "$status"},
                    "n": {"$sum": 1}}},
    ]))
    by_vendor: Dict[str, Dict[str, int]] = {}
    for r in rows:
        v = str(r["_id"].get("vendor") or "?")
        by_vendor.setdefault(v, {})[str(r["_id"].get("status") or "?")] = r["n"]
    return {"total": col.estimated_document_count(), "by_vendor": by_vendor}


# ---------------------------------------------------------------------------
# discovery_queue
# ---------------------------------------------------------------------------

def enqueue_discovery(company_id: str, *, priority: int = 1,
                      reason: str = "") -> None:
    now = _now()
    _col(QUEUE).update_one(
        {"company_id": company_id},
        {"$set": {"priority": int(priority), "reason": reason[:120]},
         "$setOnInsert": {"company_id": company_id, "state": DISCOVERY_PENDING,
                          "created_at": now, "attempts": 0,
                          "lease_owner": None, "lease_expiry": None}},
        upsert=True)


def refill_queue(limit: int = 5000) -> int:
    """Top the queue up with the highest-priority companies not yet probed.

    Pull-based rather than push-based: registry ingest writes millions of
    `companies` rows and must not also write millions of queue rows. The queue
    stays small and is refilled from the priority index.
    """
    col = _col(COMPANIES)
    cur = col.find(
        {"discovery_status": DISCOVERY_PENDING},
        {"company_id": 1, "priority": 1, "name": 1},
    ).sort("priority", -1).limit(int(limit))
    n = 0
    for doc in cur:
        enqueue_discovery(doc["company_id"],
                          priority=int(doc.get("priority") or 1),
                          reason="registry")
        n += 1
    return n


def claim_discovery_batch(owner: str, *, size: int = 25) -> List[Dict[str, Any]]:
    """Lease the next batch. ADR-003 lease semantics: at most one worker per item.

    `find_one_and_update` per item rather than a bulk claim, because the atomic
    compare-and-set on `lease_expiry` is what makes the claim safe against two
    warm containers racing; a bulk update cannot report which documents it won.
    """
    now = _now()
    expiry = now + timedelta(seconds=LEASE_TTL_SECONDS)
    col = _col(QUEUE)
    out: List[Dict[str, Any]] = []
    for _ in range(max(1, int(size))):
        doc = col.find_one_and_update(
            {"state": {"$in": [DISCOVERY_PENDING, DISCOVERY_RUNNING]},
             "$or": [{"lease_expiry": None}, {"lease_expiry": {"$lt": now}}]},
            {"$set": {"state": DISCOVERY_RUNNING, "lease_owner": owner,
                      "lease_expiry": expiry, "claimed_at": now},
             "$inc": {"attempts": 1}},
            sort=[("priority", -1), ("created_at", 1)],
            return_document=True if _return_after() is None else _return_after(),
        )
        if not doc:
            break
        out.append(doc)
    return out


def _return_after():
    try:
        from pymongo import ReturnDocument
        return ReturnDocument.AFTER
    except Exception:  # pragma: no cover
        return None


def complete_discovery(company_id: str, *, state: str,
                       endpoints_found: int = 0, detail: str = "") -> None:
    now = _now()
    _col(QUEUE).update_one(
        {"company_id": company_id},
        {"$set": {"state": state, "finished_at": now, "lease_owner": None,
                  "lease_expiry": None, "endpoints_found": int(endpoints_found),
                  "detail": detail[:300]}})
    _col(COMPANIES).update_one(
        {"company_id": company_id},
        {"$set": {"discovery_status": state, "last_discovered_at": now,
                  "endpoints_found": int(endpoints_found)}})


def queue_depth() -> Dict[str, int]:
    col = _col(QUEUE)
    return {str(r["_id"]): r["n"] for r in col.aggregate([
        {"$group": {"_id": "$state", "n": {"$sum": 1}}}])}


# ---------------------------------------------------------------------------
# Seeding from the legacy Python catalogs
# ---------------------------------------------------------------------------

def seed_from_catalogs(*, dry_run: bool = False) -> Dict[str, int]:
    """Import the hand-maintained catalogs as validated endpoints.

    These rows were validated by `validate_workday_tenants.py` /
    `validate_career_tenants.py` against live endpoints, so they enter as
    ``validated`` rather than ``candidate`` - re-proving them would waste a full
    discovery pass on 2,340 boards already known to work.
    """
    from services.ats_providers.base import Endpoint

    stats = {"workday": 0, "career": 0, "enterprise": 0, "companies": 0}
    seen_company: set = set()

    def _company(name: str) -> str:
        cid = company_id_for(name, "")
        if cid not in seen_company and not dry_run:
            upsert_company({"name": name, "company_id": cid,
                            "source": "catalog",
                            "signals": {"known_domain": True}})
            seen_company.add(cid)
            stats["companies"] += 1
        return cid

    try:
        from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
    except Exception:
        WORKDAY_TENANT_CATALOG = []
    for row in WORKDAY_TENANT_CATALOG:
        name = str(row.get("display_name") or row.get("tenant") or "")
        ep = Endpoint(vendor="workday", tenant=str(row.get("tenant") or ""),
                      site=str(row.get("site") or "External"),
                      display_name=name,
                      industry=str(row.get("industry") or "other"),
                      status=STATUS_VALIDATED,
                      extra={"wd_host": str(row.get("wd_host") or "wd1")})
        if not ep.tenant:
            continue
        if not dry_run:
            upsert_endpoint(ep, company_id=_company(name), display_name=name,
                            industry=ep.industry, source="catalog")
        stats["workday"] += 1

    try:
        from services.career_pages_catalog import CAREER_PAGES_CATALOG
    except Exception:
        CAREER_PAGES_CATALOG = []
    for row in CAREER_PAGES_CATALOG:
        name = str(row.get("display_name") or row.get("slug") or "")
        ep = Endpoint(vendor=str(row.get("ats") or ""),
                      tenant=str(row.get("slug") or ""), display_name=name,
                      industry=str(row.get("industry") or "other"),
                      status=STATUS_VALIDATED)
        if not ep.vendor or not ep.tenant:
            continue
        if not dry_run:
            upsert_endpoint(ep, company_id=_company(name), display_name=name,
                            industry=ep.industry, source="catalog")
        stats["career"] += 1

    try:
        from services.career_pages_service import ENTERPRISE_CATALOG
    except Exception:
        ENTERPRISE_CATALOG = []
    for row in ENTERPRISE_CATALOG:
        name = str(row.get("display_name") or row.get("slug") or "")
        vendor = str(row.get("ats") or "")
        # ORC rows carry the pod in `host` and the site in `extra.site_number`;
        # the provider keys on the pod hostname, so mirror that here or the
        # seeded endpoint would not match what discovery later produces.
        extra = dict(row.get("extra") or {})
        host = str(row.get("host") or "")
        if vendor == "orc":
            vendor = "oracle_orc"
        ep = Endpoint(vendor=vendor,
                      tenant=(host.split(".")[0] if vendor == "oracle_orc"
                              else str(row.get("slug") or "")),
                      site=str(extra.get("site_number") or ""),
                      host=host, display_name=name,
                      industry=str(row.get("industry") or "other"),
                      status=STATUS_VALIDATED, extra=extra)
        if not ep.vendor or not ep.tenant:
            continue
        if not dry_run:
            upsert_endpoint(ep, company_id=_company(name), display_name=name,
                            industry=ep.industry, source="catalog")
        stats["enterprise"] += 1

    return stats


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

def ensure_indexes() -> List[str]:
    """Idempotent index creation. Mirrors the style of scripts/migrate_indexes.py.

    `companies` is the largest collection in the database, so every access path
    it supports is indexed and none of them is a COLLSCAN: lookup by id, the
    priority sweep that feeds the queue, and the name join used to merge sources.
    """
    from pymongo import ASCENDING, DESCENDING

    made: List[str] = []
    companies = _col(COMPANIES)
    for spec, kwargs in (
        ([("company_id", ASCENDING)], {"unique": True, "name": "company_id_uniq"}),
        ([("discovery_status", ASCENDING), ("priority", DESCENDING)],
         {"name": "discovery_priority"}),
        ([("name_normalized", ASCENDING), ("state", ASCENDING)],
         {"name": "name_state"}),
        ([("state", ASCENDING)], {"name": "state"}),
        ([("domain", ASCENDING)], {"name": "domain", "sparse": True}),
    ):
        made.append(companies.create_index(spec, **kwargs))

    endpoints = _col(ENDPOINTS)
    for spec, kwargs in (
        ([("member_key", ASCENDING)], {"unique": True, "name": "member_key_uniq"}),
        ([("vendor", ASCENDING), ("status", ASCENDING)], {"name": "vendor_status"}),
        ([("company_id", ASCENDING)], {"name": "company", "sparse": True}),
        ([("status", ASCENDING), ("last_checked_at", ASCENDING)],
         {"name": "revalidation"}),
    ):
        made.append(endpoints.create_index(spec, **kwargs))

    queue = _col(QUEUE)
    for spec, kwargs in (
        ([("company_id", ASCENDING)], {"unique": True, "name": "company_uniq"}),
        ([("state", ASCENDING), ("priority", DESCENDING),
          ("created_at", ASCENDING)], {"name": "claim_order"}),
        ([("lease_expiry", ASCENDING)], {"name": "lease", "sparse": True}),
    ):
        made.append(queue.create_index(spec, **kwargs))
    return made
