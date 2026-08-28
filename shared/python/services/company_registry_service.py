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
import os
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
#: Reached, but the answer was not ours to trust — the site rate-limited us, a
#: bot wall answered, or the domain would not resolve. Deliberately NOT
#: `no_portal`: that state is a CLAIM about the employer ("this company has no
#: careers page") and is never retried, so recording it after a 429 writes a
#: false negative into the registry permanently. `refill_queue` picks these up
#: again like any pending row.
DISCOVERY_DEFERRED = "deferred"
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
    # This company builds software, chips or cloud infrastructure - i.e. it is
    # an employer of the engineers this product is mostly for. Confirmed from
    # the SEC-assigned SIC code, which is the registrant's own filing rather
    # than our guess. Weighted below `h1b` (a proven sponsor beats a plausible
    # one) and above `sam`, so within any tier the software employers drain
    # first. See `classify_tech`.
    "software": 40,
    # Hardware, components, telecom, instruments and medical devices: heavy
    # embedded/firmware/cloud hiring, but not primarily software companies.
    "tech_adjacent": 15,
    "sam": 20,
    "known_domain": 15,
    # Name-only evidence ("... Technologies LLC"). Deliberately much weaker
    # than the SIC-confirmed signal: the state registries are full of
    # single-member shells whose names say "Technologies" and whose careers
    # page does not exist. It is enough to sort them ahead of an unscored
    # neighbour, not enough to outrank a company with a real filing signal.
    "software_name": 12,
    "multi_state": 10,
    "recent_filing": 5,
}

# ---------------------------------------------------------------------------
# Tech classification
# ---------------------------------------------------------------------------
#
# SEC-assigned SIC codes. These come from the registrant's own filings
# (`data.sec.gov/submissions/CIK*.json`), so unlike a name heuristic they are
# not something we inferred.
#
# The two sets are deliberately different in kind, not just in degree:
#   SOFTWARE      - the company's product IS software, a chip, or a cloud
#                   service. Frontend/backend/ML/cloud/embedded hiring.
#   TECH_ADJACENT - the company builds electronics, networks or instruments.
#                   Real engineering hiring, but a minority of it is software.
SOFTWARE_SIC: frozenset = frozenset({
    "7370",   # Services-Computer Programming, Data Processing, Etc.
    "7371",   # Services-Computer Programming, Data Processing
    "7372",   # Services-Prepackaged Software
    "7373",   # Services-Computer Integrated Systems Design
    "7374",   # Services-Computer Processing & Data Preparation
    "7375",   # Services-Information Retrieval Services
    "7377",   # Services-Computer Rental & Leasing
    "7378",   # Services-Computer Maintenance & Repair
    "7379",   # Services-Computer Programming (misc)
    "3571",   # Electronic Computers
    "3572",   # Computer Storage Devices
    "3575",   # Computer Terminals
    "3576",   # Computer Communications Equipment
    "3577",   # Computer Peripheral Equipment
    "3674",   # Semiconductors & Related Devices
    "3559",   # Special Industry Machinery (semiconductor capital equipment)
})
TECH_ADJACENT_SIC: frozenset = frozenset({
    "3661", "3663", "3669",           # comms equipment
    "3670", "3672", "3675", "3676",   # electronic components / PCBs
    "3677", "3678", "3679",
    "3812", "3823", "3825", "3826", "3827", "3829",   # instruments
    "3841", "3845",                   # medical devices (embedded + cloud)
    "4813", "4899",                   # telecom services
    "7389",                           # services-computer/business, mixed bag
})

# Description keywords, used when a SIC arrives that the sets above do not
# name. The description is published alongside the code by SEC, so this is
# still the registrant's own classification rather than an inference.
_SOFTWARE_SIC_WORDS = re.compile(
    r"(prepackaged software|computer programming|data processing|"
    r"computer integrated systems|information retrieval|electronic computers|"
    r"computer storage|computer peripheral|computer communications|"
    r"semiconductor)", re.IGNORECASE)

# Name evidence, for the ~74,000 rows that come from state registries and have
# no SIC at all. Word-boundary matched, and deliberately narrow: "solutions",
# "group", "ventures" and "consulting" are NOT here, because they carry no
# information about whether anyone writes code.
_SOFTWARE_NAME = re.compile(
    r"\b(software|technologies|technology|systems|semiconductor|"
    r"microelectronics|computing|computer|cyber|cybersecurity|analytics|"
    r"informatics|robotics|automation|cloud|datacenter|data\s?center|"
    r"artificial\s+intelligence|machine\s+learning|\bai\b|\bml\b|"
    r"digital|internet|networks?|telecom|biotech|labs?|interactive|"
    r"applications?|platforms?)\b", re.IGNORECASE)


def classify_tech(name: str = "", sic: Any = None,
                  sic_description: str = "") -> Optional[str]:
    """"software" | "tech_adjacent" | None.

    SIC first, because it is the company's own filed classification; the name
    is consulted only when there is no SIC, and it produces the weaker
    `software_name` signal rather than this one (see `signals_for_company`).
    """
    # The DESCRIPTION is checked first, and only for the software case. It is
    # the more specific evidence: SEC ships it alongside the code, and where
    # the two disagree it is because a code covers a broad bucket ("7389,
    # Services-Computer Programming, Data Processing, Etc.") that this file
    # placed in `TECH_ADJACENT_SIC` for its median member. A registrant whose
    # own published description says "computer programming" is a software
    # employer whatever bucket its code sits in.
    if sic_description and _SOFTWARE_SIC_WORDS.search(str(sic_description)):
        return "software"
    code = re.sub(r"\D", "", str(sic or ""))[:4]
    if code:
        if code in SOFTWARE_SIC:
            return "software"
        if code in TECH_ADJACENT_SIC:
            return "tech_adjacent"
    return None


def signals_for_company(record: Dict[str, Any]) -> Dict[str, Any]:
    """The tech signals implied by a company record.

    Split out of `upsert_company` so the same rule serves the ingest path and
    the backfill, and so a test can exercise it without Mongo.
    """
    out: Dict[str, Any] = {}
    kind = classify_tech(str(record.get("name") or ""),
                         record.get("sic"),
                         str(record.get("sic_description") or ""))
    if kind:
        out[kind] = True
    elif not record.get("sic") and _SOFTWARE_NAME.search(str(record.get("name") or "")):
        # No filed classification to go on. Name evidence only, and priced as
        # such - it moves a row up its own tier, it does not promote it into
        # the tier above.
        out["software_name"] = True
    return out


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
    # Derived, not supplied: whether this employer builds software is a fact
    # about the record (its SIC, its name), so every writer gets it without
    # having to know to pass it. See `signals_for_company`.
    signals.update(signals_for_company(record))
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

#: States a queue row may be RE-ARMED out of. `done` and `no_portal` are
#: conclusions about the employer and are never retried here (revalidation of a
#: known endpoint is a different job, on `REVALIDATE_AFTER_DAYS`).
_REARMABLE_STATES: Tuple[str, ...] = (DISCOVERY_DEFERRED, DISCOVERY_FAILED)

#: How long a `deferred` company waits before discovery tries it again. It was
#: deferred because the answer was not ours to trust (a 429, a bot wall, a DNS
#: blip), so retrying is right - retrying every fifteen minutes is not.
DISCOVERY_RETRY_AFTER_DAYS = int(
    os.getenv("DISCOVERY_RETRY_AFTER_DAYS", "7") or 7)


def enqueue_discovery(company_id: str, *, priority: int = 1,
                      reason: str = "", rearm: bool = False) -> bool:
    """Put a company on the discovery queue. True when it is now CLAIMABLE.

    `rearm` is the difference between a queue row existing and a queue row
    being work. Without it this function is `$setOnInsert` only, so a row that
    has already run keeps its terminal state forever and re-enqueuing it is a
    no-op that still reports success. That is not hypothetical: measured on
    live `aspirely_db` (2026-08-27) the queue held 7,380 rows - 4,437
    `deferred`, 2,059 `no_portal`, 872 `done`, 12 `running` - and **zero**
    `pending`. `claim_discovery_batch` only claims `pending`/`running`, so
    every one of the 16 workers on every 15-minute tick claimed nothing, while
    `refill_queue` reported 7,368 rows "refilled". Discovery had been running
    at 4 companies/day against a 75,124-company backlog: ~51 years.
    """
    now = _now()
    update: Dict[str, Any] = {
        "$set": {"priority": int(priority), "reason": reason[:120]},
        "$setOnInsert": {"company_id": company_id, "state": DISCOVERY_PENDING,
                         "created_at": now, "attempts": 0,
                         "lease_owner": None, "lease_expiry": None},
    }
    if not rearm:
        _col(QUEUE).update_one({"company_id": company_id}, update, upsert=True)
        return False

    # Re-arm in one atomic step, guarded on the state so a row another worker
    # is holding (`running`, live lease) is never yanked out from under it.
    res = _col(QUEUE).update_one(
        {"company_id": company_id, "state": {"$in": list(_REARMABLE_STATES)}},
        {"$set": {"priority": int(priority), "reason": reason[:120],
                  "state": DISCOVERY_PENDING, "lease_owner": None,
                  "lease_expiry": None, "rearmed_at": now}})
    if res.matched_count:
        return True
    # Not present, or in a state we must not touch. Upsert covers the first.
    res = _col(QUEUE).update_one({"company_id": company_id}, update, upsert=True)
    return bool(res.upserted_id)


def refill_queue(limit: int = 5000) -> int:
    """Make the highest-priority un-probed companies CLAIMABLE. Returns how many.

    Pull-based rather than push-based: registry ingest writes millions of
    `companies` rows and must not also write millions of queue rows. The queue
    stays small and is refilled from the priority index.

    Two things here are load-bearing and both were wrong before:

    * **Deferred companies are re-armed, not merely re-visited.** The
      `DISCOVERY_DEFERRED` docstring says "refill_queue picks these up again
      like any pending row" and that was simply not true - see
      `enqueue_discovery`. They are picked up once their cooldown has elapsed.

    * **The sort has a tiebreak.** `sort("priority", -1)` with a hard `limit`
      selects the SAME rows on every tick forever, so a lower-priority tail is
      not merely served late, it is never served at all. Live proof: 4,436
      companies sat at priority 30 and 74,477 at priority 1, and a 1,600-row
      refill budget could not physically reach past the priority-30 block. The
      ascending `last_discovered_at` tiebreak puts never-probed companies (the
      field is absent, which sorts first) ahead of ones we have already tried,
      so the window moves every tick instead of standing still.

    The return value counts rows that became claimable, NOT rows visited. The
    old count reported 7,368 while creating no work at all, which is precisely
    how a fully stalled pipeline looked healthy.
    """
    now = _now()
    cutoff = now - timedelta(days=max(0, DISCOVERY_RETRY_AFTER_DAYS))
    col = _col(COMPANIES)
    query = {"$or": [
        {"discovery_status": DISCOVERY_PENDING},
        {"discovery_status": DISCOVERY_DEFERRED,
         "$or": [{"last_discovered_at": None},
                 {"last_discovered_at": {"$exists": False}},
                 {"last_discovered_at": {"$lt": cutoff}}]},
    ]}
    cur = col.find(
        query, {"company_id": 1, "priority": 1, "name": 1},
    ).sort([("priority", -1), ("last_discovered_at", 1)]).limit(int(limit))
    n = 0
    for doc in cur:
        if enqueue_discovery(doc["company_id"],
                             priority=int(doc.get("priority") or 1),
                             reason="registry", rearm=True):
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
