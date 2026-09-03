"""Coverage, ingestion, inventory and search metrics for the tech feed.

One snapshot document per run in `tech_feed_metrics` (TTL 90 days). A
dashboard reads this collection; it does not re-derive the numbers, so a
dashboard cannot drift from the definition.

Every metric here is DEFINED by the query that produces it, and the
definitions are mirrored in `docs/coverage/employer-coverage-definition.md`
sec. 3. When the two disagree, this file is the one that ran.

Read-only apart from writing its own snapshot. Safe to run on a schedule or by
hand:

    PYTHONPATH=shared/python python -c \
      "from services import tech_feed_metrics as m; m.write_snapshot()"
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

METRICS_COLLECTION = "tech_feed_metrics"
SNAPSHOT_RETENTION_DAYS = 90

#: Crawl lag above this is reported as stale. Chosen against the measured
#: cadence: every source refreshed within 14 minutes on 2026-09-02, so 90
#: minutes is ~6x the worst observed interval and will not cry wolf.
STALE_SOURCE_MINUTES = 90
#: The heartbeat is the ONLY thing separating "quiet night" from "the box is
#: wedged" (docs/ops/ingest-runner-migration.md). Alarm on it.
HEARTBEAT_ALARM_MINUTES = 5


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _facet(coll, field: str, match: Optional[Dict[str, Any]] = None,
           limit: int = 40) -> Dict[str, int]:
    pipeline: List[Dict[str, Any]] = []
    if match:
        pipeline.append({"$match": match})
    pipeline += [{"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
                 {"$sort": {"n": -1}}, {"$limit": limit}]
    return {str(r["_id"]): int(r["n"])
            for r in coll.aggregate(pipeline, allowDiskUse=True)}


# ---------------------------------------------------------------------------

def inventory_metrics(db=None) -> Dict[str, Any]:
    db = _db() if db is None else db
    P = db.job_postings
    active = {"status": "active"}
    total_active = P.count_documents(active)
    now = _now()

    out: Dict[str, Any] = {
        "active_total": total_active,
        "postings_total": P.estimated_document_count(),
        "by_status": _facet(P, "status"),
        "by_source": _facet(P, "source", active),
        "by_ats": _facet(P, "ats", active),
        "by_tech_scope": _facet(P, "tech.tech_scope", active),
        "by_role_family": _facet(P, "tech.role_family", active),
        "by_legacy_role_family": _facet(P, "role_family", active),
        "by_work_mode": _facet(P, "work_mode", active),
        "by_seniority": _facet(P, "seniority", active),
        "closed_by_reason": _facet(P, "closed_reason",
                                   {"status": {"$in": ["closed", "expired"]}}),
    }
    for days in (1, 7, 30):
        cut = now - timedelta(days=days)
        out[f"active_posted_{days}d"] = P.count_documents(
            {**active, "posted_at": {"$gte": cut},
             "posted_at_confidence": {"$ne": "unknown"}})

    # Completeness. Reported as a MISSING rate, because that is the number a
    # product decision is made on.
    fields = {
        "salary": {"compensation": {"$exists": True, "$ne": None}},
        "locations": {"locations.0": {"$exists": True}},
        "description": {"description_ref": {"$nin": [None, ""]}},
        "requirements": {"requirements": {"$exists": True, "$ne": None}},
        "posted_at": {"posted_at": {"$ne": None}},
        "work_mode": {"work_mode": {"$ne": "unknown"}},
        "seniority": {"seniority": {"$ne": "unknown"}},
        "employment_type": {"employment_type": {"$nin": [None, ""]}},
        "tech_classification": {"tech.taxonomy_version": {"$exists": True}},
    }
    out["missing_field_rate"] = {
        name: _pct(total_active - P.count_documents({**active, **q}), total_active)
        for name, q in fields.items()
    }
    out["unknown_posting_date_rate"] = _pct(
        P.count_documents({**active, "posted_at_confidence": "unknown"}),
        total_active)

    missing_desc_by_ats: Dict[str, Any] = {}
    for ats, n in (out.get("by_ats") or {}).items():
        if not n:
            continue
        missing = P.count_documents({
            **active, "ats": ats,
            "description_ref": {"$in": [None, ""]},
        })
        missing_desc_by_ats[ats] = {
            "active": n,
            "missing_description_ref": missing,
            "missing_rate": _pct(missing, n),
        }
    out["missing_description_by_ats"] = missing_desc_by_ats

    # Duplicate rate = EXCESS rows, not rows in a duplicated group. Two copies
    # of one job is one duplicate, not two.
    dup = list(P.aggregate([
        {"$match": active},
        {"$group": {"_id": "$content_hash", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
        {"$group": {"_id": None, "groups": {"$sum": 1}, "rows": {"$sum": "$n"}}},
    ], allowDiskUse=True))
    if dup:
        excess = int(dup[0]["rows"]) - int(dup[0]["groups"])
        out["duplicate_groups"] = int(dup[0]["groups"])
        out["duplicate_excess_rows"] = excess
        out["duplicate_rate"] = _pct(excess, total_active)
    else:
        out["duplicate_groups"] = out["duplicate_excess_rows"] = 0
        out["duplicate_rate"] = 0.0

    closed = P.count_documents({"status": {"$in": ["closed", "expired"]}})
    out["stale_closed_rate"] = _pct(closed, out["postings_total"])

    # One ATS tenant must be one employer. See `employer_identity`.
    conflicts = list(P.aggregate([
        {"$match": {**active, "tenant": {"$nin": [None, ""]}}},
        {"$group": {"_id": {"a": "$ats", "t": "$tenant"},
                    "names": {"$addToSet": "$employer_name"},
                    "n": {"$sum": 1}}},
        {"$match": {"$expr": {"$gt": [{"$size": "$names"}, 1]}}},
        {"$group": {"_id": None, "tenants": {"$sum": 1}, "rows": {"$sum": "$n"}}},
    ], allowDiskUse=True))
    rows = int(conflicts[0]["rows"]) if conflicts else 0
    out["employer_name_conflict_tenants"] = (int(conflicts[0]["tenants"])
                                             if conflicts else 0)
    out["employer_name_conflict_rate"] = _pct(rows, total_active)
    out["distinct_employers_active"] = len(P.distinct("employer_id", active))
    return out


def ingestion_metrics(db=None) -> Dict[str, Any]:
    db = _db() if db is None else db
    now = _now()
    out: Dict[str, Any] = {"sources": [], "alarms": []}

    for row in db.job_source_state.aggregate([
            {"$group": {"_id": "$source",
                        "last_success_at": {"$max": "$last_success_at"},
                        "shards": {"$sum": 1},
                        "in_backoff": {"$sum": {"$cond": [
                            {"$gt": [{"$ifNull": ["$retry_count", 0]}, 0]}, 1, 0]}}}},
            {"$sort": {"_id": 1}}]):
        last = row.get("last_success_at")
        lag = None
        if isinstance(last, datetime):
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            lag = int((now - last).total_seconds() // 60)
        stale = lag is None or lag > STALE_SOURCE_MINUTES
        out["sources"].append({
            "source": row["_id"], "shards": row.get("shards"),
            "crawl_lag_minutes": lag, "stale": stale,
            "shards_in_backoff": row.get("in_backoff") or 0,
        })
        if stale:
            out["alarms"].append(f"source {row['_id']} is stale (lag={lag})")

    beat = db.ingest_runner_heartbeat.find_one(sort=[("updated_at", -1)])
    age = None
    if beat and isinstance(beat.get("updated_at"), datetime):
        hb = beat["updated_at"]
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        age = int((now - hb).total_seconds() // 60)
    out["ingest_runner_heartbeat_age_minutes"] = age
    out["ingest_runner_healthy"] = age is not None and age <= HEARTBEAT_ALARM_MINUTES
    if not out["ingest_runner_healthy"]:
        out["alarms"].append(
            f"ingest runner heartbeat is {age} minutes old"
            if age is not None else "no ingest runner heartbeat recorded")

    runs = list(db.job_ingestion_runs.aggregate([
        {"$match": {"started_at": {"$gte": now - timedelta(hours=24)}}},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}}]))
    out["runs_24h"] = {str(r["_id"]): int(r["n"]) for r in runs}
    return out


def coverage_metrics(db=None) -> Dict[str, Any]:
    db = _db() if db is None else db
    now = _now()
    E = db.company_ats_endpoints
    C = db.companies
    total_ep = E.estimated_document_count()
    validated = E.count_documents({"status": "validated"})
    out: Dict[str, Any] = {
        "employer_universe_total": C.estimated_document_count(),
        "companies_by_discovery_status": _facet(C, "discovery_status"),
        "companies_with_domain": C.count_documents({"domain": {"$nin": [None, ""]}}),
        "endpoints_total": total_ep,
        "endpoints_by_status": _facet(E, "status"),
        "endpoints_by_vendor": _facet(E, "vendor"),
        "validated_endpoints_by_vendor": _facet(E, "vendor", {"status": "validated"}),
        "endpoint_validation_rate": _pct(validated, total_ep),
        "endpoints_failing": E.count_documents({"consecutive_failures": {"$gt": 0}}),
    }
    for hours, label in ((24, "24h"), (24 * 7, "7d")):
        cut = now - timedelta(hours=hours)
        out[f"discovery_throughput_{label}"] = C.count_documents(
            {"last_discovered_at": {"$gte": cut}})
        out[f"endpoint_discovery_{label}"] = E.count_documents(
            {"created_at": {"$gte": cut}})

    # Yield per board, per vendor - the number the provider priority matrix
    # is ranked on. Core-tech yield needs the taxonomy backfill to be exact;
    # until then it counts only rows that already carry `tech`.
    by_vendor_posts = _facet(db.job_postings, "ats", {"status": "active"})
    by_vendor_core = _facet(db.job_postings, "ats",
                            {"status": "active", "tech.tech_scope": "core"})
    yields = {}
    for vendor, boards in out["validated_endpoints_by_vendor"].items():
        key = str(vendor).replace("_", "-")
        posts = by_vendor_posts.get(key, by_vendor_posts.get(vendor, 0))
        core = by_vendor_core.get(key, by_vendor_core.get(vendor, 0))
        yields[vendor] = {
            "boards": boards, "active_postings": posts, "core_tech": core,
            "jobs_per_board": round(posts / boards, 1) if boards else None,
            "core_per_board": round(core / boards, 2) if boards else None,
            "core_share": _pct(core, posts),
        }
    out["source_yield_by_vendor"] = yields
    return out


def search_metrics(db=None, *, hours: int = 24) -> Dict[str, Any]:
    """Latency, zero-result rate and cursor health from recorded sessions.

    Derived from `tech_feed_searches` rather than from a separate telemetry
    pipeline: the session document already records what was asked and what came
    back, so there is nothing extra to instrument and nothing that can disagree
    with what the user actually got.
    """
    db = _db() if db is None else db
    cut = _now() - timedelta(hours=hours)
    sessions = list(db.tech_feed_searches.find(
        {"created_at": {"$gte": cut}},
        {"_id": 0, "total": 1, "diagnostics": 1, "warnings": 1,
         "applied_filters.sort": 1}))
    if not sessions:
        return {"searches": 0}

    latencies = sorted(float((s.get("diagnostics") or {}).get("total_ms") or 0)
                       for s in sessions)
    n = len(latencies)

    def pctl(p: float) -> float:
        if not latencies:
            return 0.0
        return round(latencies[min(n - 1, int(p * n))], 1)

    zero = sum(1 for s in sessions if not int(s.get("total") or 0))
    truncated = sum(1 for s in sessions
                    if any("Narrow the filters" in str(w)
                           for w in (s.get("warnings") or [])))
    classified = [(s.get("diagnostics") or {}) for s in sessions]
    read_time = [d for d in classified if d.get("candidates")]
    return {
        "searches": n,
        "search_p50_ms": pctl(0.5),
        "search_p95_ms": pctl(0.95),
        "search_max_ms": round(latencies[-1], 1),
        "search_zero_result_rate": _pct(zero, n),
        "candidate_truncation_rate": _pct(truncated, n),
        "read_time_classified_rate": (
            round(sum(d.get("read_time_classified", 0) for d in read_time)
                  / max(1, sum(d.get("candidates", 0) for d in read_time)), 4)
            if read_time else None),
    }


def ownership_metrics(db=None) -> Dict[str, Any]:
    """Employer identity and career-source ownership, from the new registries.

    Reported ALONGSIDE `coverage_metrics`, never instead of it.
    `coverage_metrics` counts `company_ats_endpoints.status == "validated"`,
    which means the endpoint answered; this counts sources whose OWNERSHIP is
    proven. Publishing both is the point: the gap between them is the number
    that used to be reported as coverage.
    """
    db = _db() if db is None else db
    out: Dict[str, Any] = {}
    try:
        from services import career_source_service as css
        from services import employer_registry as er
        out["employers"] = er.counts()
        out["career_sources"] = css.counts()
    except Exception as exc:      # pragma: no cover - a new collection may be absent
        out["error"] = type(exc).__name__
    try:
        from services import tech_feed_feedback
        out["feedback"] = tech_feed_feedback.counts()
    except Exception as exc:      # pragma: no cover
        out["feedback_error"] = type(exc).__name__
    return out


def snapshot(db=None) -> Dict[str, Any]:
    """Every metric, in one document. Read-only."""
    db = _db() if db is None else db
    return {
        "captured_at": _now(),
        "inventory": inventory_metrics(db),
        "ingestion": ingestion_metrics(db),
        "coverage": coverage_metrics(db),
        "ownership": ownership_metrics(db),
        "search": search_metrics(db),
    }


def write_snapshot(db=None) -> Dict[str, Any]:
    """Compute and persist one snapshot. The only write in this module."""
    db = _db() if db is None else db
    doc = snapshot(db)
    doc["expire_at"] = doc["captured_at"] + timedelta(days=SNAPSHOT_RETENTION_DAYS)
    try:
        db[METRICS_COLLECTION].insert_one(dict(doc))
        db[METRICS_COLLECTION].create_index("expire_at", expireAfterSeconds=0,
                                            name="metrics_ttl")
        db[METRICS_COLLECTION].create_index([("captured_at", -1)],
                                            name="captured_at_desc")
    except Exception as exc:  # pragma: no cover - a lost snapshot is not an outage
        logger.warning("tech_feed_metrics: snapshot write failed (%s)",
                       type(exc).__name__)
    doc.pop("_id", None)
    return doc


def alarms(db=None) -> List[str]:
    """The conditions that should page someone, in one call."""
    db = _db() if db is None else db
    out: List[str] = list(ingestion_metrics(db).get("alarms") or [])
    inv = inventory_metrics(db)
    if inv["duplicate_rate"] > 0.10:
        out.append(f"duplicate rate {inv['duplicate_rate']:.1%} > 10%")
    if inv["employer_name_conflict_rate"] > 0.05:
        out.append("employer-name conflicts affect "
                   f"{inv['employer_name_conflict_rate']:.1%} of active postings")
    if inv["unknown_posting_date_rate"] > 0.20:
        out.append(f"unknown posting dates {inv['unknown_posting_date_rate']:.1%} > 20%")
    cov = coverage_metrics(db)
    if cov["endpoint_validation_rate"] < 0.70:
        out.append(f"endpoint validation rate {cov['endpoint_validation_rate']:.1%} < 70%")
    if cov["discovery_throughput_24h"] < 100:
        out.append(f"discovery touched only {cov['discovery_throughput_24h']} "
                   "companies in 24h")
    srch = search_metrics(db)
    if srch.get("searches") and srch.get("search_p95_ms", 0) > 800:
        out.append(f"search p95 {srch['search_p95_ms']:.0f} ms > 800 ms")
    if srch.get("searches") and srch.get("search_zero_result_rate", 0) > 0.25:
        out.append(f"zero-result rate {srch['search_zero_result_rate']:.1%} > 25%")
    return out
