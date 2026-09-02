"""One inventory-first search for software and technical employment.

Replaces the three live-crawl fan-outs (`/jobs/workday/search`,
`/jobs/career-pages/search`, `/jobs/mnc/search`) with a single read of the
canonical `job_postings` inventory.

What it is NOT
--------------
* It does not crawl. A user request never touches an employer's site. Every
  row served was ingested by the ADR-003/004 background crawlers.
* It does not run an LLM per posting. There is no model in the hot path at
  all; classification happened at ingest and is read from `tech.*`.
* It does not add a second match score. `fit_score` (ADR-002, `fit-v2.0`)
  remains the ONE candidate-to-job score. Ordering preferences live in a
  separate `rank_score`, exactly as `jobpilot-backend/CLAUDE.md` requires.

Retrieval order (the mission's, in this order and no other)
-----------------------------------------------------------
1. hard eligibility + exclusions          - Mongo, indexed
2. core tech-scope constraint             - Mongo where backfilled, read-time
                                            classification otherwise
3. structured facets                      - Mongo, indexed
4. lexical title/skill retrieval          - Mongo + in-process scoring
5. resume fit                             - `fit_score`, in process
6. freshness / source quality / preference- `rank_score`, in process
7. semantic retrieval                     - NOT IMPLEMENTED. See sec. "Why no
                                            vector index" below.

Why no vector index
-------------------
`job_inventory_service.find_active_fresh` already records the reasoning and
this module keeps it: nothing in this repo benchmarks a recall or latency win
from one, and an unbenchmarked search index is a standing cost against an
unproven benefit. `evals/eval_tech_feed_retrieval.py` is the harness that
would have to show a win first, against the lexical/faceted baseline this
module IS.

Pagination
----------
One bounded retrieval per SEARCH, not per page. The ordered `job_id` list, the
facets and the inventory watermark are written to `tech_feed_searches` (TTL),
and a cursor is `{search_id, offset}` base64'd. Pages after the first are a
slice plus a `$in` fetch - no re-query, no re-rank, no re-scoring.

That is deliberately not a keyset over `posted_at`. Relevance ordering is
computed in process from `fit_score`, so there is no Mongo sort key to keyset
on; and keysetting over `posted_at` while the crawlers insert rows underneath
produces exactly the duplicates-and-omissions-across-pages failure the
acceptance criteria forbid. Recording the ordering once, under a stated
watermark, is what makes a page stable.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

FEED_VERSION = "tech-feed-1.0.0"

#: How many postings one SEARCH considers. Everything after this is not
#: reachable by paging, and the response says so (`warnings`), rather than
#: pretending the market ends here.
CANDIDATE_LIMIT = int(os.getenv("TECH_FEED_CANDIDATES", "2000") or 2000)
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25
#: Ordered ids kept per search session. Bounds the session document and
#: therefore the depth a cursor can reach.
MAX_ORDERED = int(os.getenv("TECH_FEED_MAX_ORDERED", "2000") or 2000)
SESSION_TTL_SECONDS = int(os.getenv("TECH_FEED_SESSION_TTL", "3600") or 3600)
SESSIONS_COLLECTION = "tech_feed_searches"

WINDOW_DAYS_DEFAULT = 30
WINDOW_DAYS_MAX = 180

SORT_RELEVANCE = "relevance"
SORT_NEWEST = "newest"
SORT_SALARY = "salary"
SORTS = (SORT_RELEVANCE, SORT_NEWEST, SORT_SALARY)

EMPLOYER_SCOPE_ANY = "any"
EMPLOYER_SCOPE_TECH = "tech_only"
EMPLOYER_SCOPES = (EMPLOYER_SCOPE_ANY, EMPLOYER_SCOPE_TECH)

APPLY_MODE_ANY = "any"
APPLY_MODE_DIRECT = "direct_apply"

EMPLOYER_KIND_ANY = "any"
EMPLOYER_KIND_DIRECT = "direct_employer"
EMPLOYER_KIND_STAFFING = "staffing"
EMPLOYER_KIND_MARKETPLACE = "marketplace"
EMPLOYER_KINDS = (EMPLOYER_KIND_ANY, EMPLOYER_KIND_DIRECT,
                  EMPLOYER_KIND_STAFFING, EMPLOYER_KIND_MARKETPLACE)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _sessions():
    return _db()[SESSIONS_COLLECTION]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Request normalization
# ---------------------------------------------------------------------------

def _str_list(value: Any, *, limit: int, max_len: int = 120,
              lower: bool = False) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for item in value:
        s = str(item or "").strip()[:max_len]
        if not s:
            continue
        if lower:
            s = s.lower()
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _int_or_none(value: Any, *, lo: int, hi: int) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _float_or_none(value: Any, *, lo: float, hi: float) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _clamp_page_size(value: Any) -> int:
    # `0`/`None`/`""` mean "unset", not "a page of zero jobs".
    if value in (None, "", 0, False):
        return DEFAULT_PAGE_SIZE
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    if n < 1:
        return DEFAULT_PAGE_SIZE
    return min(n, MAX_PAGE_SIZE)


def _as_utc(value: Any) -> Any:
    """BSON round-trips datetimes as NAIVE UTC (pymongo is not `tz_aware` here).

    A watermark read back from a session is therefore not comparable to one
    computed in process, and "is this page from the same watermark?" silently
    answers no. Normalize on the way out so the value the client sees is the
    same value on page 1 and page 9.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def normalize_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Untrusted body -> a validated, bounded, echo-able filter set.

    Every hard constraint is deterministic and appears verbatim in
    `applied_filters`, so the user is shown what actually ran rather than what
    they asked for. Anything unrecognised is dropped, not passed through - a
    Mongo filter must never contain a key this function did not write.

    On `InputSanitizer` (CLAUDE.md invariant 4). This function is a WHITELIST,
    which is strictly stronger than sanitizing: every output key is a literal
    written here, every value is coerced to `str`/`int`/`float`/`bool` and
    bounded, and enum-valued fields are checked against their allowed set. A
    dict or list a caller smuggles in becomes a string in a VALUE position and
    can never reach an operator position - `tests/test_tech_feed_search.py::
    test_query_operators_cannot_reach_mongo` walks the built filter and
    asserts no leaf is a dict and no leaf starts with `$`.
    """
    from services import tech_taxonomy as tx
    p = payload if isinstance(payload, dict) else {}

    scopes = [s for s in _str_list(p.get("tech_scope"), limit=4, lower=True)
              if s in tx.SCOPES]
    families = [f for f in _str_list(p.get("role_families"), limit=12, lower=True)
                if tx.is_valid_family(f)]
    specialties = [s for s in _str_list(p.get("role_specialties"), limit=24, lower=True)
                   if tx.is_valid_specialty(s)]
    # A specialty implies its family; asking for `sre` without saying
    # `cloud_platform` must not return nothing.
    for f in tx.families_for_specialties(specialties):
        if f not in families:
            families.append(f)

    sort = str(p.get("sort") or SORT_RELEVANCE).strip().lower()
    if sort not in SORTS:
        sort = SORT_RELEVANCE

    employer_scope = str(p.get("employer_scope") or EMPLOYER_SCOPE_ANY).strip().lower()
    if employer_scope not in EMPLOYER_SCOPES:
        employer_scope = EMPLOYER_SCOPE_ANY

    employer_kind = str(p.get("employer_kind") or EMPLOYER_KIND_ANY).strip().lower()
    if employer_kind not in EMPLOYER_KINDS:
        employer_kind = EMPLOYER_KIND_ANY

    work_modes = [w for w in _str_list(p.get("work_modes"), limit=4, lower=True)
                  if w in ("remote", "hybrid", "onsite", "unknown")]
    seniorities = [s for s in _str_list(p.get("seniority"), limit=8, lower=True)
                   if s in ("intern", "entry", "mid", "senior", "staff",
                            "principal", "director", "executive", "unknown")]

    req: Dict[str, Any] = {
        # --- intent ---------------------------------------------------------
        "query": str(p.get("query") or "").strip()[:300],
        "titles": _str_list(p.get("titles"), limit=40),
        "role_families": families,
        "role_specialties": specialties,
        "tech_scope": scopes or [tx.SCOPE_CORE],
        # --- skills ----------------------------------------------------------
        "skills_required": _str_list(p.get("skills_required"), limit=30,
                                     max_len=60, lower=True),
        "skills_preferred": _str_list(p.get("skills_preferred"), limit=30,
                                      max_len=60, lower=True),
        # --- exclusions -------------------------------------------------------
        "exclude_skills": _str_list(p.get("exclude_skills"), limit=30,
                                    max_len=60, lower=True),
        "exclude_title_terms": _str_list(p.get("exclude_title_terms"), limit=30,
                                         max_len=80),
        "exclude_keywords": _str_list(p.get("exclude_keywords"), limit=30,
                                      max_len=80),
        "exclude_companies": _str_list(p.get("exclude_companies"), limit=60,
                                       max_len=120),
        # --- geography ---------------------------------------------------------
        "countries": _str_list(p.get("countries"), limit=10, max_len=40, lower=True),
        "regions": _str_list(p.get("regions"), limit=20, max_len=60),
        "cities": _str_list(p.get("cities"), limit=20, max_len=80),
        "radius_km": _int_or_none(p.get("radius_km"), lo=0, hi=500),
        "work_modes": work_modes,
        "remote_eligible_locations": _str_list(
            p.get("remote_eligible_locations"), limit=10, max_len=60),
        "timezone_overlap_hours": _int_or_none(p.get("timezone_overlap_hours"),
                                               lo=0, hi=12),
        # --- role shape --------------------------------------------------------
        "seniority": seniorities,
        "min_years": _int_or_none(p.get("min_years"), lo=0, hi=40),
        "max_years": _int_or_none(p.get("max_years"), lo=0, hi=40),
        "employment_types": _str_list(p.get("employment_types"), limit=8,
                                      max_len=40, lower=True),
        # --- compensation --------------------------------------------------------
        "min_salary": _float_or_none(p.get("min_salary"), lo=0, hi=10_000_000),
        "salary_currency": (str(p.get("salary_currency") or "").strip().upper()[:3]
                            or None),
        "pay_interval": (str(p.get("pay_interval") or "").strip().lower()[:10]
                         or None),
        "include_unknown_salary": (True if p.get("include_unknown_salary") is None
                                   else bool(p.get("include_unknown_salary"))),
        # --- employer ------------------------------------------------------------
        "employer_scope": employer_scope,
        "employer_kind": employer_kind,
        "company_ids": _str_list(p.get("company_ids"), limit=200, max_len=120,
                                 lower=True),
        "company_categories": _str_list(p.get("company_categories"), limit=20,
                                        max_len=40, lower=True),
        # --- eligibility -----------------------------------------------------------
        "sponsorship_preference": (
            str(p.get("sponsorship_preference") or "any").strip().lower()[:20]),
        "work_authorization": _str_list(p.get("work_authorization"), limit=6,
                                        max_len=40, lower=True),
        "requires_clearance": (None if p.get("requires_clearance") is None
                               else bool(p.get("requires_clearance"))),
        # --- freshness / apply -------------------------------------------------------
        "posted_within_days": _int_or_none(p.get("posted_within_days"),
                                           lo=0, hi=WINDOW_DAYS_MAX)
                              or WINDOW_DAYS_DEFAULT,
        "include_undated": (True if p.get("include_undated") is None
                            else bool(p.get("include_undated"))),
        "apply_mode": (APPLY_MODE_DIRECT
                       if str(p.get("apply_mode") or "").strip().lower()
                       == APPLY_MODE_DIRECT else APPLY_MODE_ANY),
        # --- output ---------------------------------------------------------------
        "sort": sort,
        # CLAMP, do not reject. `_int_or_none` returns None for out-of-range,
        # so `page_size: 1000` silently became 25 - a client asking for a big
        # page got a small one with no indication why.
        "page_size": _clamp_page_size(p.get("page_size")),
        "personalize": bool(p.get("personalize", True)),
        "resume_id": str(p.get("resume_id") or "").strip()[:64] or None,
        "user_email": str(p.get("user_email") or "").strip()[:200],
        "tz_offset_minutes": _int_or_none(p.get("tz_offset_minutes"),
                                          lo=-840, hi=840) or 0,
    }
    if req["min_years"] is not None and req["max_years"] is not None \
            and req["max_years"] < req["min_years"]:
        req["max_years"] = req["min_years"]
    return req


def filters_fingerprint(req: Dict[str, Any]) -> str:
    """Stable hash of everything that changes the RESULT SET.

    Excludes presentation-only fields (`page_size`, `tz_offset_minutes`), so
    changing the page size reuses the session rather than re-running the
    search - and INCLUDES `sort`, because a different order is a different
    ordered list and a cursor into the old one would be meaningless.
    """
    ignore = {"page_size", "tz_offset_minutes"}
    payload = {k: v for k, v in sorted(req.items()) if k not in ignore}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------

#: How far past the ranked window one keyset page reads before giving up.
#: The hard filters run in Python, so a page of 25 may need several hundred
#: raw rows; this bounds that without bounding the TRAVERSAL, which is the
#: whole point of the keyset phase.
KEYSET_OVERFETCH = int(os.getenv("TECH_FEED_KEYSET_OVERFETCH", "8") or 8)
KEYSET_MAX_SCAN = int(os.getenv("TECH_FEED_KEYSET_MAX_SCAN", "1500") or 1500)

#: Ceiling on the exact-match count. Counting 222,000 documents on every
#: search is not worth the latency, so above this the total is reported as a
#: floor and `total_is_exact` says so - a number that might be a floor and
#: does not admit it is worse than no number.
COUNT_LIMIT = int(os.getenv("TECH_FEED_COUNT_LIMIT", "50000") or 50000)


def encode_cursor(search_id: str, offset: Optional[int] = None, *,
                  keyset: Optional[Dict[str, Any]] = None) -> str:
    """Two cursor shapes, one encoding.

    `{"s": id, "o": offset}` pages the RANKED order recorded at search time —
    stable, personalised, and bounded by `MAX_ORDERED` because the order lives
    in a session document.

    `{"s": id, "k": {"p": posted_at, "j": job_id}}` continues PAST that window
    by keyset on the same `(posted_at desc, job_id asc)` the retrieval index
    is built for. It is what removes the 2,000-result traversal ceiling: the
    ranked window is a memory bound on how many rows can be SCORED at once,
    and it was silently doubling as a bound on how many could be REACHED.
    """
    if keyset is not None:
        posted = keyset.get("p")
        obj: Dict[str, Any] = {
            "s": search_id,
            "k": {"p": posted.isoformat() if isinstance(posted, datetime) else None,
                  "j": str(keyset.get("j") or "")[:64]},
        }
    else:
        obj = {"s": search_id, "o": int(offset or 0)}
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Any) -> Optional[Tuple[str, int, Optional[Dict[str, Any]]]]:
    """`(search_id, offset, keyset)` or None. Never raises on client input."""
    s = str(cursor or "").strip()
    if not s or len(s) > 512:
        return None
    try:
        pad = "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode(s + pad)
        obj = json.loads(raw.decode("utf-8"))
        sid = str(obj["s"])[:64]
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        return None
    if not sid:
        return None

    if isinstance(obj.get("k"), dict):
        raw_posted = obj["k"].get("p")
        posted = _as_utc(_parse_iso(raw_posted)) if raw_posted else None
        job_id = str(obj["k"].get("j") or "")[:64]
        if raw_posted and posted is None:
            return None
        return sid, 0, {"p": posted, "j": job_id}

    try:
        off = int(obj["o"])
    except (ValueError, KeyError, TypeError):
        return None
    if off < 0 or off > MAX_ORDERED:
        return None
    return sid, off, None


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Stage 1-3: the Mongo query (hard eligibility, tech scope, structured facets)
# ---------------------------------------------------------------------------

def build_query(req: Dict[str, Any], *, now: Optional[datetime] = None
                ) -> Dict[str, Any]:
    """The indexed part of the filter. Everything expressible in Mongo is here.

    Nothing user-supplied reaches an operator position: every value passed
    through `normalize_request` first, and only keys written by THIS function
    appear in the filter (`utils.security` invariant 4).
    """
    from services import job_inventory_service as inv
    from services import tech_taxonomy as tx

    now = now or _now()
    cutoff = now - timedelta(days=max(0, int(req["posted_within_days"])))

    q: Dict[str, Any] = {"status": inv.STATUS_ACTIVE}

    # --- freshness. Undated rows ride on `first_seen_at`, which is a real
    #     lower bound we observed rather than a date we invented (ADR-001).
    if req["include_undated"]:
        q["$or"] = [
            {"posted_at": {"$gte": cutoff},
             "posted_at_confidence": {"$ne": inv.CONFIDENCE_UNKNOWN}},
            {"posted_at_confidence": inv.CONFIDENCE_UNKNOWN,
             "first_seen_at": {"$gte": cutoff}},
        ]
    else:
        q["posted_at"] = {"$gte": cutoff}
        q["posted_at_confidence"] = {"$ne": inv.CONFIDENCE_UNKNOWN}

    # --- tech scope.
    #
    # `tech.*` is written at ingest but 227,914 rows predate it. Until the
    # backfill completes, a query on `tech.tech_scope` alone returns only the
    # newly-crawled slice - which would read as "the market shrank by 95%".
    # So the scope clause is an OR of the STORED verdict and the legacy
    # `role_family` values that can plausibly carry core-tech work; rows
    # arriving via the legacy branch are re-classified in `_read_time_scope`
    # and dropped there if they do not hold up. Precision is preserved, and
    # the widening disappears on its own once every row carries `tech`.
    scopes = list(req["tech_scope"])
    scope_clause: List[Dict[str, Any]] = [{"tech.tech_scope": {"$in": scopes}}]
    if tx.SCOPE_CORE in scopes or tx.SCOPE_ADJACENT in scopes:
        scope_clause.append({
            "tech": {"$exists": False},
            "role_family": {"$in": list(tx.LEGACY_TECH_CANDIDATE_FAMILIES)},
        })
    _and: List[Dict[str, Any]] = [{"$or": scope_clause}]

    # --- families. Same dual-registry problem, same treatment.
    if req["role_families"]:
        fam_clause: List[Dict[str, Any]] = [
            {"tech.role_families": {"$in": req["role_families"]}}]
        legacy = tx.legacy_families_for(req["role_families"])
        if legacy:
            fam_clause.append({"tech": {"$exists": False},
                               "role_family": {"$in": legacy}})
        _and.append({"$or": fam_clause})
    if req["role_specialties"]:
        _and.append({"$or": [
            {"tech.role_specialties": {"$in": req["role_specialties"]}},
            {"tech": {"$exists": False}},
        ]})

    if req["work_modes"]:
        q["work_mode"] = {"$in": req["work_modes"]}
    if req["seniority"]:
        # Unknown rides along unless the caller asked for `unknown` alone:
        # most postings state no level, and an equality filter would hide the
        # majority of the market behind a distinction the employer never drew.
        wanted = set(req["seniority"])
        if wanted != {"unknown"}:
            wanted.add("unknown")
        q["seniority"] = {"$in": sorted(wanted)}
    if req["employment_types"]:
        # Read the NORMALISED field. Filtering on the raw provider spelling
        # meant `full_time` matched "full-time" and missed "fulltime",
        # "regular-full-time", "ft-hourly-w-bn" and "cdi" - 3,700+ live rows
        # of the same employment type under four other names. Rows the
        # normalisation backfill has not reached carry no normalised value, so
        # they are ORed in rather than excluded: an unnormalised row is one we
        # have not read yet, not a part-time job.
        from services.job_inventory_service import normalize_employment_type
        wanted = sorted({normalize_employment_type(v) for v in req["employment_types"]}
                        - {"unknown"})
        if wanted:
            _and.append({"$or": [
                {"employment_type_normalized": {"$in": wanted}},
                {"employment_type_normalized": {"$exists": False}},
            ]})
    if req["company_ids"]:
        # A company may be named by its normalized employer id or by its ATS
        # tenant slug; the UI hands back whichever the catalog gave it.
        _and.append({"$or": [{"employer_id": {"$in": req["company_ids"]}},
                             {"tenant": {"$in": req["company_ids"]}}]})
    if req["cities"]:
        keys = [inv.normalize_location(c)["key"] for c in req["cities"]]
        keys = [k for k in keys if k]
        if keys:
            q["locations.key"] = {"$in": keys}
    if req["regions"]:
        # Normalised through the same location parser as `cities`, so
        # "TX" and "Texas" and "tx" all reach the same stored value. It was
        # accepted, echoed in `applied_filters`, and never applied.
        wanted = {r.strip() for r in req["regions"] if r and r.strip()}
        variants = sorted({v for r in wanted
                           for v in (r, r.upper(), r.title(), r.lower())})
        if variants:
            _and.append({"locations.region": {"$in": variants}})
    if req["pay_interval"]:
        # `compensation.period` is the stored field; the API renames it
        # `salary.interval`. A row with NO compensation is ORed in: not
        # stating a pay interval is not the same as stating a different one.
        _and.append({"$or": [
            {"compensation.period": req["pay_interval"]},
            {"compensation": {"$exists": False}},
        ]})
    if req["countries"]:
        # Filter on the NORMALISED code, and fall back to the raw spelling for
        # rows the location backfill has not reached yet. The old form -
        # `{"locations.country": {"$in": req["countries"]}}` - compared a
        # lowercased request against a stored "USA"/"India"/"Canada" and
        # therefore matched nothing at all, on any country, ever.
        from services.job_inventory_service import normalize_country
        codes = sorted({c for c in (normalize_country(v) for v in req["countries"]) if c})
        spellings = sorted({v for v in req["countries"] if v}
                           | {v.upper() for v in req["countries"] if v}
                           | {v.title() for v in req["countries"] if v})
        clauses: List[Dict[str, Any]] = []
        if codes:
            clauses.append({"locations.country_code": {"$in": codes}})
        if spellings:
            clauses.append({"locations.country": {"$in": spellings}})
        if len(clauses) == 1:
            q.update(clauses[0])
        elif clauses:
            _and.append({"$or": clauses})

    # Salary is a RANGE on a field most rows do not have, so it cannot be a
    # plain Mongo clause without silently excluding the unpriced majority.
    # Only the explicit opt-out is expressible here; the floor itself is
    # applied in `_hard_filter`, where "unknown" can be honoured.
    if req["min_salary"] and not req["include_unknown_salary"]:
        q["compensation"] = {"$exists": True, "$ne": None}

    if _and:
        # ASSIGN, which is why every clause above appends to `_and` rather
        # than to `q["$and"]`. Two of them used `q.setdefault("$and", [])`,
        # and since `_and` always holds at least the unconditional tech-scope
        # clause, this line then overwrote them: `countries` and
        # `employment_types` were built correctly, put on the query, and
        # deleted one line later. Measured live, `countries: ["india"]`
        # returned the same 17,683 postings as no country filter at all.
        q["$and"] = _and
    return q


_PROJECTION = {
    "_id": 0, "job_id": 1, "source": 1, "source_job_id": 1, "canonical_url": 1,
    "employer_id": 1, "employer_name": 1, "title_raw": 1, "title_normalized": 1,
    "role_family": 1, "seniority": 1, "locations": 1, "work_mode": 1,
    "employment_type": 1, "posted_at": 1, "posted_at_confidence": 1,
    "first_seen_at": 1, "last_verified_at": 1, "valid_through": 1,
    "cost_class": 1, "source_quality": 1, "content_hash": 1, "requirements": 1,
    "status": 1, "description_ref": 1, "description_chars": 1,
    "industry": 1, "ats": 1, "tenant": 1, "compensation": 1,
    "tech": 1, "dedupe_group_id": 1, "variant_group_id": 1,
    "board_kind": 1,
}

#: The retrieval projection. `requirements` is DELIBERATELY absent.
#:
#: Measured against live Atlas, 2,000 candidates: 4,087 ms with
#: `requirements`, 986 ms without - it is 513 of 1,729 average bytes but 76%
#: of the wall clock, because it is a nested subdocument and the cost is BSON
#: decode, not transfer. Server-side the query itself is 60 ms
#: (`docsExamined=7152`, index-backed), so essentially all of the latency was
#: hydrating a field only the SURVIVORS need.
#:
#: So retrieval is two-phase: scan light, then hydrate `requirements` for the
#: few hundred rows that survive filtering and dedupe. The exception is a
#: request that filters ON skills, which cannot be answered without them -
#: `_needs_requirements_upfront`.
_PROJECTION_LIGHT = {k: v for k, v in _PROJECTION.items() if k != "requirements"}


def _needs_requirements_upfront(req: Dict[str, Any]) -> bool:
    """True when a HARD filter reads `requirements`, so it cannot be deferred.

    A free-text query counts. Matching it against titles alone is the
    difference between 38 and several thousand results for "kubernetes", and a
    search box that misses the roles it should find is the failure this feed
    exists to avoid. The cost is real and bounded - it applies only to requests
    that actually carry a query - and it buys the recall.
    """
    return bool(req.get("skills_required") or req.get("exclude_skills")
                or req.get("query"))


def hydrate_requirements(rows: Sequence[Dict[str, Any]]) -> int:
    """Fetch `requirements` for exactly these rows. Returns how many arrived.

    Second phase of retrieval. `fit_score` scores off `requirements` and
    nothing else, so a row without it is unscored - which is a real and honest
    state, not a failure.
    """
    from services import job_inventory_service as inv
    ids = [str(r.get("job_id")) for r in rows
           if r.get("job_id") and "requirements" not in r]
    if not ids:
        return 0
    found = {d["job_id"]: d.get("requirements")
             for d in inv._postings().find({"job_id": {"$in": ids}},
                                           {"_id": 0, "job_id": 1,
                                            "requirements": 1})}
    n = 0
    for row in rows:
        jid = row.get("job_id")
        if jid in found:
            row["requirements"] = found[jid]
            row["_requirements"] = found[jid]
            n += 1
    return n


def retrieve(req: Dict[str, Any], *, limit: Optional[int] = None,
             now: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """One bounded read. Returns `(docs, diagnostics)`.

    `limit` resolves at CALL time, not at import: a default argument bound to
    `CANDIDATE_LIMIT` froze the value at module load, so changing the constant
    (in an operational tweak or a test) had no effect on the query that
    actually ran.
    """
    from services import job_inventory_service as inv

    if limit is None:
        limit = CANDIDATE_LIMIT

    query = build_query(req, now=now)
    projection = (_PROJECTION if _needs_requirements_upfront(req)
                  else _PROJECTION_LIGHT)
    started = time.perf_counter()
    # Sorting on `posted_at` descending is what the compound indexes are built
    # for; the FINAL order is decided in `rank`, but retrieving newest-first
    # means a truncated candidate set is truncated at the OLD end, which is
    # the only truncation a job seeker would accept.
    # `(posted_at desc, job_id asc)` rather than `posted_at` alone. The second
    # key costs nothing on an index that already leads with `posted_at`, and it
    # is what makes the order TOTAL - a keyset cursor cannot resume from a
    # position that ties, and `posted_at` ties constantly (28,025 postings
    # shared one day in the live inventory).
    docs = list(inv._postings().find(query, projection)
                .sort([("posted_at", -1), ("job_id", 1)]).limit(int(limit)))
    took = (time.perf_counter() - started) * 1000

    total_matched, exact = _count_matches(query)
    diagnostics = {"retrieval_ms": round(took, 1), "candidates": len(docs),
                   "requirements_upfront": projection is _PROJECTION,
                   "query_keys": sorted(query.keys()),
                   "total_matched": total_matched, "total_is_exact": exact}
    if docs:
        last = docs[-1]
        diagnostics["_keyset"] = {"p": _as_utc(last.get("posted_at")),
                                  "j": str(last.get("job_id") or "")}
    return docs, diagnostics


def _count_matches(query: Dict[str, Any]) -> Tuple[int, bool]:
    """`(count, is_exact)` for the whole match set, bounded.

    The feed used to report `total` as the length of the RANKED WINDOW, so a
    query matching 40,000 postings said "2000" - the same number as one
    matching exactly 2,000. Counting is cheap on an index and the difference
    between "2,000" and "at least 50,000" is the difference between a feed a
    user believes and one they do not.
    """
    from services import job_inventory_service as inv
    try:
        n = inv._postings().count_documents(query, limit=COUNT_LIMIT + 1,
                                            maxTimeMS=2500)
    except Exception as exc:      # pragma: no cover - a slow count must not 500
        logger.debug("tech_feed._count_matches: %s", type(exc).__name__)
        return 0, False
    if n > COUNT_LIMIT:
        return COUNT_LIMIT, False
    return int(n), True


# ---------------------------------------------------------------------------
# Stage 2 (read-time half) + stage 4: scope, exclusions, lexical
# ---------------------------------------------------------------------------

def _read_time_tech(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The stored verdict, or one computed now from the title.

    A read-time verdict is title-only (the body lives in S3) and is therefore
    weaker than the ingest-time one, which is why it carries a lower
    confidence and why the backfill matters. It is never written back from
    here - a read path that mutates the collection it is reading is how a
    slow query becomes a slow write.
    """
    stored = doc.get("tech")
    if isinstance(stored, dict) and stored.get("taxonomy_version"):
        return stored
    from services import tech_taxonomy as tx
    verdict = tx.classify(doc.get("title_raw")).to_doc()
    verdict["classification_evidence"] = list(
        verdict.get("classification_evidence") or [])
    verdict["classification_evidence"].append(
        {"kind": "read_time_title_only", "field": "title",
         "value": "posting predates the taxonomy backfill"})
    return verdict


def _compile_terms(terms: Sequence[str]) -> Optional[re.Pattern]:
    cleaned = [re.escape(t.strip()) for t in terms if str(t or "").strip()]
    if not cleaned:
        return None
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(cleaned) + r")(?![A-Za-z0-9_])",
                      re.IGNORECASE)


def _drop_user_suppressed(docs: Sequence[Dict[str, Any]],
                          suppressed: Optional[Dict[str, Any]]
                          ) -> Tuple[List[Dict[str, Any]], int]:
    """Remove rows this user dismissed. Returns `(kept, dropped_count)`.

    `suppressed` is whatever `tech_feed_feedback.suppressed_for` returned, loaded
    ONCE per search by `_run_search`. Only preference reasons appear in it
    (`not_interested` / `irrelevant_role` / `duplicate`); data-quality reports
    such as `wrong_company` are deliberately absent and never hide a posting -
    see `tech_feed_feedback`'s module docstring.

    Matching on `dedupe_group_id` as well as `job_id` is what makes "I already
    saw this" hold: the same requisition reached through two sources is two
    `job_id`s and one group, and dismissing one copy must not leave the other.
    """
    if not suppressed:
        return list(docs), 0
    job_ids = set(suppressed.get("job_ids") or ())
    group_ids = set(suppressed.get("dedupe_group_ids") or ())
    if not job_ids and not group_ids:
        return list(docs), 0
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for doc in docs:
        gid = str(doc.get("dedupe_group_id") or "")
        if str(doc.get("job_id") or "") in job_ids or (gid and gid in group_ids):
            dropped += 1
            continue
        kept.append(doc)
    return kept, dropped


def hard_filter(docs: Sequence[Dict[str, Any]], req: Dict[str, Any],
                *, suppressed: Optional[Dict[str, Any]] = None
                ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Stages 1-4 that Mongo cannot express. Returns `(kept, drop_reasons)`.

    Exclusions run FIRST and unconditionally. Nothing below can readmit a row
    an exclusion removed - the same invariant `test_filter_exclusions.py` pins
    for the daily pipeline, for the same reason.

    `suppressed` (optional) is the caller-supplied per-user suppression set;
    `None` for a guest, in which case nothing is looked up or dropped.
    """
    from services import tech_taxonomy as tx
    from services import employer_tech_classifier as etc

    exclude_titles = _compile_terms(req["exclude_title_terms"])
    exclude_keywords = _compile_terms(req["exclude_keywords"])
    exclude_companies = {c.lower() for c in req["exclude_companies"]}
    exclude_skills = set(req["exclude_skills"])
    required_skills = set(req["skills_required"])
    title_terms = _compile_terms(req["titles"]) if req["titles"] else None
    query_words = [t for t in re.split(r"[\s,]+", req["query"]) if len(t) >= 3] \
        if req["query"] else []
    query_terms = _compile_terms(query_words) if query_words else None
    # Each word compiled SEPARATELY, so the search can require all of them.
    # `query_terms` (the any-of pattern) stays for the `titles` fallback below,
    # which is a different question: "did the free-text box rescue a row the
    # title list would have dropped".
    query_each = [_compile_terms([w]) for w in query_words]
    wanted_scopes = set(req["tech_scope"])
    wanted_specialties = set(req["role_specialties"])
    wanted_families = set(req["role_families"])

    drops: Dict[str, int] = {}
    skills_unknown = 0

    def drop(reason: str) -> None:
        drops[reason] = drops.get(reason, 0) + 1

    # === BEGIN user suppression (tech_feed_feedback) - ONE block ============
    # Rows this user explicitly dismissed, removed before every other stage for
    # the same reason exclusions are: nothing below may readmit them. The set is
    # loaded once per search by `_run_search` and passed in, so this stays a
    # pure function of its arguments and a guest search costs nothing here.
    docs, _n_suppressed = _drop_user_suppressed(docs, suppressed)
    if _n_suppressed:
        drops["user_suppressed"] = _n_suppressed
    # === END user suppression ==============================================

    kept: List[Dict[str, Any]] = []
    for doc in docs:
        title = str(doc.get("title_raw") or "")
        company = str(doc.get("employer_name") or "")

        if exclude_companies and company.lower() in exclude_companies:
            drop("excluded_company"); continue
        if exclude_titles and exclude_titles.search(title):
            drop("excluded_title_term"); continue

        tech = _read_time_tech(doc)
        doc["_tech"] = tech
        if tech.get("tech_scope") not in wanted_scopes:
            drop("tech_scope"); continue
        if wanted_families:
            fams = set(tech.get("role_families") or ([tech.get("role_family")]
                                                     if tech.get("role_family") else []))
            if fams and not (fams & wanted_families):
                drop("role_family"); continue
        if wanted_specialties:
            specs = set(tech.get("role_specialties") or [])
            if specs and not (specs & wanted_specialties):
                drop("role_specialty"); continue

        skills = _skills_of(doc, tech)
        if exclude_skills and (skills & exclude_skills):
            drop("excluded_skill"); continue
        if required_skills and not (required_skills <= skills):
            # Only enforced when we KNOW the posting's skills. A row we could
            # not parse must not be excluded for failing a test it was never
            # given - that is the unknown-is-not-a-no rule this codebase
            # applies to dates and seniority. The count of rows skipped for
            # THIS reason is reported separately, so a filter that is silently
            # doing nothing is visible instead of looking like it worked.
            if skills:
                drop("missing_required_skill"); continue
            drop("skills_unknown_kept"); skills_unknown += 1

        if exclude_keywords and exclude_keywords.search(title):
            drop("excluded_keyword"); continue

        # === free-text query ==================================================
        # The search box was compiled and then read in exactly ONE place: as a
        # fallback when `titles` was ALSO set. With no title list - the normal
        # case - typing "kubernetes" narrowed nothing at all. Measured live:
        # 17,683 postings with the box empty, 17,683 with "kubernetes" in it.
        #
        # What is searched: the title, the employer, the taxonomy verdict
        # (families and specialties) and the EXTRACTED SKILLS. NOT the raw job
        # description - the body lives in S3 behind `description_ref` and
        # scanning it would mean an object fetch per candidate. Skills are what
        # make this usable: title-only, "kubernetes" returned 38 of 17,688
        # core-tech postings, which is not a search result, it is a shortfall.
        # It is why `_needs_requirements_upfront` counts a query.
        #
        # ALL words must appear, each somewhere in that haystack. Any-of turns
        # "senior kubernetes" into every senior role in the index.
        if query_each:
            hay = " ".join([
                title, str(doc.get("title_normalized") or ""), company,
                " ".join(str(f) for f in (tech.get("role_families") or [])),
                " ".join(str(x) for x in (tech.get("role_specialties") or [])),
                str(tech.get("role_family") or ""),
                " ".join(sorted(skills)),
            ])
            if not all(pat.search(hay) for pat in query_each if pat):
                drop("query_mismatch"); continue

        if title_terms and not title_terms.search(title):
            if not (query_terms and query_terms.search(title)):
                drop("title_mismatch"); continue

        # "Min years" / "Max years" are a range on what the POSTING asks for,
        # so both compare against `required_years_min` - the number the
        # employer actually stated. `min_years` used to read
        # `required_years_max`, which is populated only when a JD spells out a
        # RANGE ("3-5 years") and is therefore set on 0 of 224,389 live
        # postings: the filter was accepted, echoed, and could never fire.
        #
        # A posting that states no requirement is KEPT by both, as everywhere
        # else here: not stating a number is not the same as failing one.
        years_min = tech.get("required_years_min")
        if isinstance(years_min, int):
            if req["max_years"] is not None and years_min > req["max_years"]:
                drop("requires_more_years"); continue
            if req["min_years"] is not None and years_min < req["min_years"]:
                drop("below_requested_experience"); continue

        comp = doc.get("compensation") or {}
        top, plausible = _salary_band(doc)
        doc["_salary_top"] = top
        doc["_salary_plausible"] = plausible
        if req["min_salary"]:
            if top is None:
                if not req["include_unknown_salary"]:
                    drop("salary_unknown" if plausible else "salary_implausible")
                    continue
            elif top < req["min_salary"]:
                drop("salary_below_floor"); continue
        elif not req["include_unknown_salary"] and top is None:
            drop("salary_unknown" if plausible else "salary_implausible"); continue
        if req["salary_currency"] and comp.get("currency") \
                and str(comp["currency"]).upper() != req["salary_currency"]:
            drop("salary_currency"); continue

        if req["employer_kind"] != EMPLOYER_KIND_ANY:
            kind = _employer_kind_of(doc)
            if kind != req["employer_kind"]:
                drop("employer_kind"); continue

        if req["apply_mode"] == APPLY_MODE_DIRECT and not _is_direct_apply(doc):
            drop("not_direct_apply"); continue

        if req["requires_clearance"] is False and _mentions_clearance(title):
            drop("clearance_required"); continue

        # Display aliases. `inventory_feed._dedupe` and `_score_rows` are
        # shared with the legacy tabs and read `company` / `title` /
        # `location`; a raw inventory doc has `employer_name` / `title_raw` /
        # `locations`. Without this mapping every row hashed to the SAME
        # legacy dedupe key ("legacy|||") and the whole result set collapsed
        # to one card - measured, not hypothetical.
        doc["company"] = company
        doc["title"] = title
        locs = doc.get("locations") or []
        doc["location"] = (str(locs[0].get("raw") or "")
                           if locs and isinstance(locs[0], dict) else "")
        doc["_requirements"] = doc.get("requirements")

        kept.append(doc)
    # `skills_unknown_kept` is an accounting line, not a removal. Subtract it
    # so `filtered_out` sums to rows actually dropped.
    drops.pop("skills_unknown_kept", None)
    if skills_unknown:
        drops["_skills_unknown_kept"] = skills_unknown
    return kept, drops


_MARKETPLACE_HINT_RE = re.compile(
    r"(jobgether|jobot|dice|ziprecruiter|talent\.com|adzuna|jooble|"
    r"whatjobs|lensa|snagajob)", re.IGNORECASE)
_STAFFING_HINT_RE = re.compile(
    r"(staffing|consulting group|recruit(?:ing|ment)|talent solutions|"
    r"workforce solutions|manpower|randstad|adecco|kforce|robert half|"
    r"insight global|teksystems|apex systems|collabera|infosys bpm|"
    r"cybercoders|motion recruitment|beacon hill)", re.IGNORECASE)
_CLEARANCE_RE = re.compile(
    r"(?<![a-z])(ts/sci|top secret|secret clearance|security clearance|"
    r"polygraph|poly\b|q clearance|l clearance|public trust)", re.IGNORECASE)


def _skills_of(doc: Dict[str, Any], tech: Dict[str, Any]) -> Set[str]:
    """Every skill we know this posting asks for, lowercased.

    `requirements` uses `required_skills` / `preferred_skills`
    (`fit_score.extract_requirements`). An earlier version read
    `requirements["skills"]`, which does not exist - so with `tech.*` also
    unbackfilled the skill set was empty for every row, and BOTH skill filters
    silently matched everything. Measured: `skills_required=["kubernetes"]`
    returned 435 results, identical to no filter at all.
    """
    out = {str(s).lower() for s in (tech.get("skills_required") or [])}
    out |= {str(s).lower() for s in (tech.get("skills_preferred") or [])}
    reqs = doc.get("requirements")
    if isinstance(reqs, dict):
        for key in ("required_skills", "preferred_skills", "skills"):
            out |= {str(s).lower() for s in (reqs.get(key) or [])}
    return {s for s in out if s}


#: Salary plausibility, measured rather than assumed.
#:
#: `salary_normalizer` scrapes pay out of free text and gets it wrong on a
#: small but highly visible slice. Live inventory, 30,019 priced active
#: postings with an `annual_max`:
#:
#:     p50  $72,800     p90  $208,000    p95  $310,000
#:     p99  $2,079,979  p99.9 $6,813,600  max  $10,000,000
#:
#: 2.4% claim more than $1M/year. Every sampled one is a parse failure, and
#: the raw text says how:
#:
#:     raw "\n\n630-5"        -> "$5 - $630 an hour"      (a page fragment)
#:     raw "000.00 - $123,500" -> "$123,500 a month"       (split mid-number)
#:     raw "500.00 - $215,000" -> "$500 - $215,000 a month"
#:
#: Sorting by salary put all of them on the first page, which is the most
#: visible possible way to look broken.
#:
#: The ROOT CAUSE is in `salary_normalizer`, on the ingest path, and fixing it
#: means re-parsing 42,333 stored rows - a separate migration. This is the
#: read-side guard: an implausible figure is treated as UNKNOWN for filtering
#: and sorting, while the employer's own text is still displayed and the card
#: says the number is not trusted.
SALARY_SANITY_ANNUAL_MAX = float(
    os.getenv("TECH_FEED_SALARY_SANITY_MAX", "1000000") or 1000000)
#: Per-period ceilings, checked against the period the parser CLAIMED. A
#: monthly figure above $60k is an annual figure mislabelled; $250/hr is a real
#: contract rate and $630/hr is not.
SALARY_SANITY_BY_PERIOD = {
    "hour": 250.0, "hourly": 250.0,
    "day": 5000.0, "daily": 5000.0,
    "week": 15000.0, "weekly": 15000.0,
    "month": 60000.0, "monthly": 60000.0,
}
#: A band wider than this is two unrelated numbers, not a range. Real bands sit
#: near 1.3x-2x and even deliberately wide ones rarely reach 4x;
#: "$500 - $215,000" is 430x and "$5 - $630" is 126x.
SALARY_SANITY_SPREAD = 12.0


def _salary_band(doc: Dict[str, Any]) -> Tuple[Optional[float], bool]:
    """`(comparable annual figure, plausible)`.

    An implausible band is NOT deleted - the employer's own text is still
    shown - but it is treated as UNKNOWN for filtering and sorting, and the
    card carries `salary.plausible: false` so the surface can say why.
    """
    comp = doc.get("compensation") or {}
    lo, hi = comp.get("annual_min"), comp.get("annual_max")
    top = hi if hi is not None else lo
    if top is None:
        return None, True
    try:
        top = float(top)
        low = float(lo) if lo is not None else None
    except (TypeError, ValueError):
        return None, False

    if top <= 0 or top > SALARY_SANITY_ANNUAL_MAX:
        return None, False
    if low is not None and low > top:
        return None, False
    if low is not None and low > 0 and (top / low) > SALARY_SANITY_SPREAD:
        return None, False

    # The raw figures against the period the parser claimed. `period` is the
    # stored field name (`interval` is what the API calls it).
    period = str(comp.get("period") or comp.get("interval") or "").lower()
    ceiling = SALARY_SANITY_BY_PERIOD.get(period)
    if ceiling is not None:
        for raw in (comp.get("max"), comp.get("min")):
            try:
                if raw is not None and float(raw) > ceiling:
                    return None, False
            except (TypeError, ValueError):
                return None, False
    return top, True


def _employer_kind_of(doc: Dict[str, Any]) -> str:
    if str(doc.get("board_kind") or "").lower() == "marketplace":
        return EMPLOYER_KIND_MARKETPLACE
    name = str(doc.get("employer_name") or "")
    if _MARKETPLACE_HINT_RE.search(name):
        return EMPLOYER_KIND_MARKETPLACE
    if _STAFFING_HINT_RE.search(name):
        return EMPLOYER_KIND_STAFFING
    return EMPLOYER_KIND_DIRECT


def _is_direct_apply(doc: Dict[str, Any]) -> bool:
    """The posting links to the employer's own ATS, not to an aggregator.

    `board_kind` is the stored answer, but it was never written by the ingest
    path, so it is None on every posting stored before that fix - which made
    `apply_mode: direct_apply` match all 225,095 active rows, marketplace
    reposts included. The catalog lookup is the same fallback `inventory_feed`
    already uses, so both surfaces agree on which boards are aggregators.
    """
    if str(doc.get("board_kind") or "").lower() == "marketplace":
        return False
    if not doc.get("board_kind"):
        try:
            from services.career_pages_catalog import is_marketplace_board
            if is_marketplace_board({"tenant": doc.get("tenant"),
                                     "company": doc.get("employer_name")}):
                return False
        except Exception:      # pragma: no cover
            pass
    return bool(doc.get("canonical_url")) and str(doc.get("cost_class") or "") == "free"


def _mentions_clearance(text: str) -> bool:
    return bool(_CLEARANCE_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Stage 5-6: fit score, then rank score. TWO scores, never merged.
# ---------------------------------------------------------------------------

def _load_profile(req: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str], int]:
    """`(fit_score profile, skills, years)` - or `(None, [], 0)`.

    No parsed resume means NO score, per `jobpilot-backend/CLAUDE.md`: the
    card renders "Not scored" and `match_score` stays 0 with no `fit` marker.
    A number would be a claim we cannot support.
    """
    if not req["personalize"] or not req["user_email"]:
        return None, [], 0
    try:
        from services.workday_jobs_service import _load_resume_profile
        skills, years = _load_resume_profile(req["user_email"])
    except Exception as exc:  # pragma: no cover
        logger.warning("tech_feed: resume profile unavailable (%s)", type(exc).__name__)
        return None, [], 0
    if not skills:
        return None, [], 0
    try:
        # The SAME builder the inventory feed uses. Not a reimplementation:
        # `_build_profile` unions every parsed resume the user has, and a
        # second construction here would score the same candidate differently
        # on two surfaces - which is precisely what ADR-002 forbids.
        from services.inventory_feed import _build_profile
        profile = _build_profile(req["user_email"], list(skills), years)
    except Exception as exc:  # pragma: no cover
        logger.warning("tech_feed: profile build failed (%s)", type(exc).__name__)
        return None, list(skills), int(years or 0)
    return profile, list(skills), int(years or 0)


def _days_ago(when: Optional[datetime], now: datetime) -> Optional[int]:
    if not isinstance(when, datetime):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (now - when).days)


#: Source quality, as an ordering preference only. A DIRECT employer board
#: outranks a marketplace repost of the same job; this is the rule the rest of
#: the feed already sorts by and it lives in `rank_score`, never in
#: `match_score`.
_SOURCE_RANK = {"free": 1.0, "paid": 0.4}


def rank(rows: List[Dict[str, Any]], req: Dict[str, Any],
         profile: Optional[Dict[str, Any]], skills: Sequence[str], years: int,
         *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Attach `match_score` (fit_score, THE score) and `rank_score` (ordering).

    They are deliberately separate fields with separate meanings:

    * `match_score` answers "how well does this candidate fit this job?" It is
      `fit_score` and nothing else, versioned by `fit_score_version`, and it
      is 0 with no version marker when there is no parsed resume.
    * `rank_score` answers "in what order should these be shown?" and folds in
      freshness, source directness, and how many of the user's own filters the
      row satisfies. It is presentation, it is not a match claim, and no
      surface may display it as one.
    """
    now = now or _now()
    from services import fit_score
    from services import inventory_feed

    # THE score, computed by the one shared engine, through the one shared
    # entry point. `_score_rows` writes `match_score`, `matched_skills`,
    # `missing_skills` and the versioned `fit` block; nothing here recomputes
    # or post-processes any of them.
    for row in rows:
        row.setdefault("_requirements", row.get("requirements"))
        row.setdefault("title", str(row.get("title_raw") or ""))
        if "location" not in row:
            locs = row.get("locations") or []
            row["location"] = (str(locs[0].get("raw") or "")
                               if locs and isinstance(locs[0], dict) else "")
    try:
        inventory_feed._score_rows(rows, req["user_email"], list(skills),
                                   years, profile=profile)
    except Exception as exc:  # pragma: no cover - an unscored feed beats a 500
        logger.warning("tech_feed: scoring failed (%s)", type(exc).__name__)
        for row in rows:
            row.setdefault("match_score", 0)

    wanted_specialties = set(req["role_specialties"])
    wanted_skills = {s.lower() for s in req["skills_required"]} | \
                    {s.lower() for s in req["skills_preferred"]}

    for row in rows:
        tech = row.get("_tech") or {}

        match = int(row.get("match_score") or 0)
        why: List[str] = []
        mismatches: List[str] = []
        # No parsed resume => NO score (CLAUDE.md invariant). `_score_rows`
        # already leaves `match_score` at 0 and writes no `fit` block; the
        # version marker must be absent too, or the card renders "Not scored"
        # next to a version that implies one was computed.
        row["fit_score_version"] = (row.get("fit") or {}).get("score_version")
        matched = row.get("matched_skills") or []
        if matched:
            why.append("Your skills: " + ", ".join(str(m) for m in matched[:5]))
        missing = row.get("missing_skills") or []
        if missing:
            mismatches.append("Missing: " + ", ".join(str(m) for m in missing[:3]))

        days = _days_ago(row.get("posted_at"), now)
        seen_days = _days_ago(row.get("first_seen_at"), now)
        freshness = 0.0
        if days is not None:
            freshness = max(0.0, 1.0 - (days / max(1, req["posted_within_days"])))
        elif seen_days is not None:
            # An undated row can still be ranked, on a weaker bound, and it is
            # capped below every dated row so "we know when" always wins.
            freshness = 0.4 * max(0.0, 1.0 - (seen_days / max(1, req["posted_within_days"])))

        source = _SOURCE_RANK.get(str(row.get("cost_class") or ""), 0.3)
        if _employer_kind_of(row) == EMPLOYER_KIND_DIRECT:
            source += 0.25
            why.append("Direct employer posting")
        elif _employer_kind_of(row) == EMPLOYER_KIND_MARKETPLACE:
            # A marketplace repost is the same job with an extra hop, an
            # unknown employer string and usually no salary. It ranks below a
            # direct board decisively, not marginally - "Jobgether" was the
            # single largest employer in the live inventory (6,075 rows) and
            # took the top of every unfiltered core-tech page.
            source -= 0.6

        preference = 0.0
        specs = set(tech.get("role_specialties") or [])
        if wanted_specialties and (specs & wanted_specialties):
            preference += 0.5
            hit = sorted(specs & wanted_specialties)[0]
            why.append(f"Specialty match: {hit}")
        row_skills = {s.lower() for s in (tech.get("skills_required") or [])} | \
                     {s.lower() for s in (tech.get("skills_preferred") or [])}
        overlap = wanted_skills & row_skills
        if overlap:
            preference += min(0.5, 0.1 * len(overlap))
            why.append("Skills: " + ", ".join(sorted(overlap)[:5]))
        if req["work_modes"] and row.get("work_mode") in req["work_modes"]:
            preference += 0.2
        conf = float(tech.get("classification_confidence") or 0.0)

        # Weights are stated here rather than tuned: this is an ordering
        # heuristic over an already-filtered set, and every component is
        # reported in `why_matched` so a user can see what moved a row up.
        row["rank_score"] = round(
            0.45 * (match / 100.0)
            + 0.20 * freshness
            + 0.15 * max(0.0, min(1.0, source))
            + 0.15 * min(1.0, preference)
            + 0.05 * conf, 4)

        if req["min_salary"] and not (row.get("compensation") or {}).get("annual_min"):
            mismatches.append("salary not published")
        if row.get("posted_at_confidence") == "unknown":
            mismatches.append("posting date unknown")
        if row.get("work_mode") == "unknown" and req["work_modes"]:
            mismatches.append("work arrangement not stated")
        row["why_matched"] = why[:6]
        row["hard_constraint_mismatches"] = mismatches[:4]
        row["_days_ago"] = days
        row["_seen_days_ago"] = seen_days

    if req["sort"] == SORT_NEWEST:
        rows.sort(key=lambda r: (_ineligible(r),
                                 r["_days_ago"] if r["_days_ago"] is not None else 10 ** 6,
                                 -(r.get("rank_score") or 0),
                                 str(r.get("job_id") or "")))
    elif req["sort"] == SORT_SALARY:
        def _pay(r: Dict[str, Any]) -> float:
            top = r.get("_salary_top")
            if top is None:
                top, _ = _salary_band(r)
            # An unpriced or implausible row sorts LAST under a salary sort,
            # not first. It is not a high-paying job; it is a job we cannot
            # price.
            return float(top) if top is not None else -1.0
        rows.sort(key=lambda r: (_ineligible(r), -_pay(r),
                                 -(r.get("rank_score") or 0),
                                 str(r.get("job_id") or "")))
    else:
        rows.sort(key=lambda r: (_ineligible(r),
                                 -(r.get("rank_score") or 0),
                                 -(r.get("match_score") or 0),
                                 str(r.get("job_id") or "")))
    return rows


def _ineligible(row: Dict[str, Any]) -> int:
    """1 for a posting the candidate provably cannot take, 0 otherwise.

    FIRST key in every sort, ahead of relevance, recency and salary. A
    posting whose hard requirement the candidate provably does not meet - or
    which is a confident cross-domain finding - does not belong above one they
    can take, however fresh it is or however the component arithmetic lands.

    `not_scored` and a missing verdict are NOT demoted: those are absences of
    evidence, and demoting on absence is the mistake the whole of fit-v2.1 is
    correcting. Only a positive `ineligible` finding sorts down.
    """
    return 1 if (row.get("fit") or {}).get("eligibility") == "ineligible" else 0


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------

_FACET_FIELDS = ("role_family", "role_specialty", "tech_scope", "seniority",
                 "work_mode", "employer", "ats", "source", "employment_type",
                 "salary_published", "country", "employer_kind")


def build_facets(rows: Sequence[Dict[str, Any]], *, top: int = 25
                 ) -> Dict[str, List[Dict[str, Any]]]:
    """Counts over the FILTERED set, so a facet count is what clicking it gives.

    Computed after `hard_filter` and before pagination: a facet computed on one
    page is a lie about the result set, and one computed before filtering is a
    lie about what the filter would return.
    """
    from collections import Counter
    counters: Dict[str, Counter] = {f: Counter() for f in _FACET_FIELDS}
    for row in rows:
        tech = row.get("_tech") or {}
        counters["tech_scope"][tech.get("tech_scope") or "unknown"] += 1
        for fam in (tech.get("role_families") or []):
            counters["role_family"][fam] += 1
        for spec in (tech.get("role_specialties") or []):
            counters["role_specialty"][spec] += 1
        counters["seniority"][tech.get("seniority") or row.get("seniority") or "unknown"] += 1
        counters["work_mode"][row.get("work_mode") or "unknown"] += 1
        counters["employer"][row.get("employer_name") or "(unknown)"] += 1
        counters["ats"][row.get("ats") or "(unknown)"] += 1
        counters["source"][row.get("source") or "(unknown)"] += 1
        counters["employment_type"][row.get("employment_type") or "(unstated)"] += 1
        comp = row.get("compensation") or {}
        counters["salary_published"][
            "published" if (comp.get("annual_min") or comp.get("annual_max"))
            else "not published"] += 1
        locs = row.get("locations") or []
        country = (locs[0].get("country") if locs and isinstance(locs[0], dict) else "") or "unknown"
        counters["country"][country] += 1
        counters["employer_kind"][_employer_kind_of(row)] += 1
    return {
        field: [{"value": v, "count": n} for v, n in c.most_common(top)]
        for field, c in counters.items()
    }


# ---------------------------------------------------------------------------
# Card shape - the canonical output contract
# ---------------------------------------------------------------------------

def _card(row: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    """One posting in the shape the contract promises. Unknown stays absent."""
    tech = row.get("_tech") or {}
    comp = row.get("compensation") or {}
    locs = [l for l in (row.get("locations") or []) if isinstance(l, dict)]
    days = row.get("_days_ago")
    seen_days = row.get("_seen_days_ago")
    url = str(row.get("canonical_url") or "")

    card = {
        "job_id": row.get("job_id"),
        "dedupe_group_id": row.get("dedupe_group_id") or row.get("job_id"),
        "employer_id": row.get("employer_id"),
        "employer_name": row.get("employer_name") or "",
        "employer_tech_classification": row.get("_employer_tech"),
        "employer_tech_evidence": row.get("_employer_tech_evidence") or [],
        "employer_kind": _employer_kind_of(row),
        "title_raw": row.get("title_raw") or "",
        "normalized_title": tech.get("normalized_title") or row.get("title_raw") or "",
        "tech_scope": tech.get("tech_scope") or "unknown",
        "role_family": tech.get("role_family"),
        "role_families": tech.get("role_families") or [],
        "role_specialties": tech.get("role_specialties") or [],
        "occupation_codes": tech.get("occupation_codes") or [],
        "taxonomy_version": tech.get("taxonomy_version"),
        "classification_confidence": tech.get("classification_confidence"),
        "classification_evidence": tech.get("classification_evidence") or [],
        "skills_required": tech.get("skills_required") or [],
        "skills_preferred": tech.get("skills_preferred") or [],
        "seniority": tech.get("seniority") or row.get("seniority") or "unknown",
        "required_years_min": tech.get("required_years_min"),
        "required_years_max": tech.get("required_years_max"),
        "work_mode": row.get("work_mode") or "unknown",
        "locations": [{"raw": l.get("raw"), "city": l.get("city"),
                       "region": l.get("region"), "country": l.get("country")}
                      for l in locs[:6]],
        # What we can honestly say about where a remote role may be worked
        # FROM. Absent unless the posting said so - "remote" is not "remote
        # anywhere", and inventing an eligibility is how a seeker wastes an
        # application.
        "remote_eligibility": row.get("remote_eligibility"),
        "employment_type": row.get("employment_type") or "",
        "salary": {
            "min_annual": comp.get("annual_min"),
            "max_annual": comp.get("annual_max"),
            "currency": comp.get("currency"),
            # STORED as `period`; the API contract calls it `interval`. Reading
            # `interval` off the document returned null on every card.
            "interval": comp.get("period") or comp.get("interval"),
            "display": comp.get("display"),
            "confidence": comp.get("confidence"),
            "published": bool(comp.get("annual_min") or comp.get("annual_max")),
            # False when the published figure cannot be a salary - almost
            # always an interval misread ("$180,000 a month"). The text is
            # still shown; the number is not trusted for filtering or sorting.
            "plausible": bool(row.get("_salary_plausible", True)),
        },
        # Both read the PARSED requirements, which the extractor already
        # produces and nothing was surfacing: `_sponsorship` was never written
        # anywhere, so every card reported `null` for a field the posting had
        # actually stated. `None` still means "the posting did not say" - the
        # absence is preserved, it is just no longer manufactured.
        "sponsorship": _sponsorship_of(row),
        "clearance_required": _clearance_of(row),
        "employment_type_normalized": row.get("employment_type_normalized"),
        "source": row.get("source"),
        "ats": row.get("ats"),
        "source_requisition_id": row.get("source_job_id"),
        "canonical_url": url,
        "apply_url": url,
        "direct_apply": _is_direct_apply(row),
        "posted_at": row.get("posted_at"),
        "posted_at_confidence": row.get("posted_at_confidence") or "unknown",
        "days_ago": days,
        "first_seen_at": row.get("first_seen_at"),
        "first_seen_days_ago": seen_days,
        "last_seen_at": row.get("last_seen_at"),
        "last_verified_at": row.get("last_verified_at"),
        "valid_through": row.get("valid_through"),
        "match_score": int(row.get("match_score") or 0),
        "fit_score_version": row.get("fit_score_version"),
        "fit": row.get("fit"),
        # PROMOTED to the top level as well as living inside `fit`. A client
        # deciding whether to render a passing band must not have to reach
        # into a nested block whose shape has changed once already; and
        # `eligibility` answers a question `match_score` cannot, so burying it
        # one level below the score is exactly the wrong place for it.
        "eligibility": (row.get("fit") or {}).get("eligibility"),
        "ineligible_reasons": (row.get("fit") or {}).get("ineligible_reasons") or [],
        "evidence_breadth": (row.get("fit") or {}).get("evidence_breadth"),
        "role_alignment": (row.get("fit") or {}).get("role_alignment") or {},
        "hard_constraints": (row.get("fit") or {}).get("hard_constraints") or [],
        "rank_score": row.get("rank_score"),
        "why_matched": row.get("why_matched") or [],
        "hard_constraint_mismatches": row.get("hard_constraint_mismatches") or [],
        "source_variants": row.get("source_variants") or [],
        "location_variants": row.get("location_variants") or [],
        "duplicate_count": row.get("duplicate_count") or 0,
    }
    for key in ("employer_name_original", "employer_name_confidence",
                "employer_name_conflict", "employer_name_variants"):
        if key in row:
            card[key] = row[key]
    return card


# ---------------------------------------------------------------------------
# Search sessions
# ---------------------------------------------------------------------------

def _save_session(*, search_id: str, req: Dict[str, Any], order: List[str],
                  facets: Dict[str, Any], watermark: Optional[datetime],
                  diagnostics: Dict[str, Any], coverage: Dict[str, Any],
                  source_health: Dict[str, Any], warnings: List[str],
                  keyset: Optional[Dict[str, Any]] = None,
                  total_matched: Optional[int] = None,
                  total_is_exact: bool = True) -> None:
    now = _now()
    try:
        _sessions().update_one(
            {"search_id": search_id},
            {"$set": {
                "search_id": search_id,
                "user_email": req.get("user_email") or "",
                "filters_hash": filters_fingerprint(req),
                "applied_filters": req,
                "order": order[:MAX_ORDERED],
                "total": len(order),
                # Where the ranked window ENDED, in index order. Paging past
                # `order` resumes here instead of stopping, which is what
                # makes the traversal unbounded while the SCORED window stays
                # bounded.
                "keyset": keyset,
                "total_matched": total_matched,
                "total_is_exact": bool(total_is_exact),
                "facets": facets,
                "inventory_watermark": watermark,
                "diagnostics": diagnostics,
                "coverage": coverage,
                "source_health": source_health,
                "warnings": warnings,
                "feed_version": FEED_VERSION,
                "created_at": now,
                "expire_at": now + timedelta(seconds=SESSION_TTL_SECONDS),
            }},
            upsert=True)
    except Exception as exc:  # pragma: no cover - a lost session costs a re-search
        logger.warning("tech_feed: session save failed (%s)", type(exc).__name__)


def _load_session(search_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    try:
        doc = _sessions().find_one({"search_id": search_id}, {"_id": 0})
    except Exception as exc:  # pragma: no cover
        logger.warning("tech_feed: session load failed (%s)", type(exc).__name__)
        return None
    if not doc:
        return None
    # A cursor is scoped to whoever created it. Search sessions carry the
    # user's filters and their ordering, which is personalized; handing one to
    # another account would leak both.
    if str(doc.get("user_email") or "") != str(user_email or ""):
        logger.warning("tech_feed: cursor rejected - session belongs to another user")
        return None
    return doc


# ---------------------------------------------------------------------------
# Coverage + source health (reported, never inferred)
# ---------------------------------------------------------------------------

#: `coverage_block` reads five collections. Recomputing that per search is
#: waste, and the numbers move on a crawl cadence, not a request cadence.
COVERAGE_TTL_SECONDS = int(os.getenv("TECH_FEED_COVERAGE_TTL", "300") or 300)
_COVERAGE_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}


def coverage_block(*, force: bool = False) -> Dict[str, Any]:
    """What the inventory can honestly claim about its own completeness.

    Three of these numbers used to overstate it, and the brief is explicit
    that we must "never promise literal coverage of every technology company"
    and "maintain a dated, measurable denominator":

    * `known_employers` was `companies.estimated_document_count()` — every
      company ever HARVESTED, ~80,000 — rendered by the banner as "N active
      postings across M employers". We do not have postings across 80,000
      employers. It is now the count of employers actually present in the
      active inventory.
    * `validated_endpoints` counted `company_ats_endpoints.status ==
      "validated"`, which means only that the endpoint answered. It is the
      number that reported 43% coverage and cannot tell a company's own board
      from a stranger's board with a similar slug (ADR-006). It is renamed to
      `responding_endpoints` — what it actually measures — and the real
      figures are reported beside it.
    * Nothing reported the DENOMINATOR at all, so no client could say what
      share of the technology employers we track is owner-verified.
    """
    from services import job_inventory_service as inv
    now = time.time()
    if not force and _COVERAGE_CACHE["value"] is not None \
            and now - float(_COVERAGE_CACHE["at"]) < COVERAGE_TTL_SECONDS:
        return dict(_COVERAGE_CACHE["value"])

    out: Dict[str, Any] = {"as_of": _now()}
    try:
        db = _db()
        out["active_postings"] = db.job_postings.count_documents(
            {"status": inv.STATUS_ACTIVE})
        # Employers we actually hold live postings for. `len(distinct(...))`
        # over an indexed field, not a scan of the postings themselves.
        try:
            out["known_employers"] = len(db.job_postings.distinct(
                "employer_id", {"status": inv.STATUS_ACTIVE}))
        except Exception:
            out["known_employers"] = 0
        out["harvested_companies"] = db.companies.estimated_document_count()
        out["responding_endpoints"] = db.company_ats_endpoints.count_documents(
            {"status": "validated"})
        try:
            from services import career_source_service as css
            out["owner_verified_sources"] = db[css.CAREER_SOURCES].count_documents(
                {"state": {"$in": list(css.COVERED_STATES)}})
            out["registered_employers"] = db[
                "employers"].estimated_document_count()
        except Exception:
            pass
    except Exception as exc:  # pragma: no cover
        logger.warning("tech_feed: coverage unavailable (%s)", type(exc).__name__)
        out["error"] = type(exc).__name__
    _COVERAGE_CACHE["at"] = now
    _COVERAGE_CACHE["value"] = dict(out)
    return out


def source_health_block(*, stale_after_minutes: int = 90) -> Dict[str, Any]:
    """Per-source crawl lag, and whether the ingest runner is alive.

    A search that quietly serves a source whose crawler died three days ago is
    the failure this exists to prevent - it looks identical to a quiet market.
    """
    out: Dict[str, Any] = {"sources": [], "warnings": []}
    now = _now()
    try:
        db = _db()
        agg = db.job_source_state.aggregate([
            {"$group": {"_id": "$source",
                        "last_success_at": {"$max": "$last_success_at"},
                        "shards": {"$sum": 1},
                        "failures": {"$sum": {"$ifNull": ["$retry_count", 0]}}}},
            {"$sort": {"_id": 1}},
        ])
        for row in agg:
            last = row.get("last_success_at")
            lag = None
            if isinstance(last, datetime):
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                lag = int((now - last).total_seconds() // 60)
            stale = lag is None or lag > stale_after_minutes
            out["sources"].append({
                "source": row["_id"], "shards": row.get("shards"),
                "last_success_at": last, "crawl_lag_minutes": lag,
                "stale": stale, "retry_count": row.get("failures") or 0,
            })
            if stale:
                out["warnings"].append(
                    f"{row['_id']}: no successful crawl in "
                    + (f"{lag} minutes" if lag is not None else "any recorded run"))
        beat = db.ingest_runner_heartbeat.find_one(sort=[("updated_at", -1)])
        if beat and isinstance(beat.get("updated_at"), datetime):
            hb = beat["updated_at"]
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            mins = int((now - hb).total_seconds() // 60)
            out["ingest_runner"] = {"last_heartbeat_at": hb,
                                    "minutes_ago": mins,
                                    "healthy": mins <= 5}
            if mins > 5:
                out["warnings"].append(
                    f"ingest runner heartbeat is {mins} minutes old")
        else:
            out["ingest_runner"] = {"last_heartbeat_at": None, "healthy": False}
            out["warnings"].append("no ingest runner heartbeat recorded")
    except Exception as exc:  # pragma: no cover
        logger.warning("tech_feed: source health unavailable (%s)", type(exc).__name__)
        out["error"] = type(exc).__name__
    return out


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _watermark(docs: Sequence[Dict[str, Any]]) -> Optional[datetime]:
    """The newest posting time in the candidate set.

    Recorded on the session and echoed on every page. It is what makes a page
    STABLE: pages 2..n are served from an ordering computed at this instant,
    so rows the crawler inserts afterwards cannot shift, duplicate or hide
    anything the user has already been shown.
    """
    best: Optional[datetime] = None
    for d in docs:
        for field in ("posted_at", "first_seen_at"):
            v = d.get(field)
            if isinstance(v, datetime):
                if v.tzinfo is None:
                    v = v.replace(tzinfo=timezone.utc)
                if best is None or v > best:
                    best = v
    return best


def _resolve_employers(rows: List[Dict[str, Any]]) -> None:
    """One name per ATS tenant, and the employer's tech verdict on the card.

    Read-time, so it is reversible and needs no migration. See
    `employer_identity` for why: 12.5% of live active postings are published
    under a name that belongs to a different company.
    """
    try:
        from services import employer_identity as ei
    except Exception:  # pragma: no cover
        return
    rmap = ei.build_resolution_map(
        {"ats": r.get("ats"), "tenant": r.get("tenant"),
         "employer_name": r.get("employer_name")} for r in rows)
    for row in rows:
        ei.apply_resolution(row, rmap, name_field="employer_name")
        # Keep the display alias in step with the resolved name, or dedupe
        # groups by the OLD name while the card shows the new one.
        row["company"] = row.get("employer_name")


def employer_categories_for(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
    """`(ats|tenant) -> employer_categories` from the registry, in one query.

    Keyed on `(ats, tenant)` for the same reason everything else here is: a
    posting's `employer_id` is a slug derived from whatever name the board
    printed. Both stored spellings of the join key are accepted while the
    registry migration is in flight.
    """
    keys: set = set()
    for r in rows:
        ten = str(r.get("tenant") or "").strip().lower()
        if ten:
            keys.add(f"{str(r.get('ats') or '').strip().lower()}:{ten}")
            keys.add(f":{ten}")
    if not keys:
        return {}
    out: Dict[str, List[str]] = {}
    try:
        db = _db()
        cur = db["employers"].find(
            {"$or": [{"registry_refs.tenant": {"$in": sorted(keys)}},
                     {"registry_refs.tenant_keys": {"$in": sorted(keys)}}]},
            {"registry_refs": 1, "employer_categories": 1})
        for doc in cur:
            cats = [str(c).lower() for c in (doc.get("employer_categories") or [])]
            refs = doc.get("registry_refs") or {}
            for k in list(refs.get("tenant") or []) + list(refs.get("tenant_keys") or []):
                if str(k) in keys:
                    out[str(k)] = cats
    except Exception as exc:  # pragma: no cover - a registry outage must not 500
        logger.warning("tech_feed: employer categories unavailable (%s)",
                       type(exc).__name__)
    return out


def filter_company_categories(rows: List[Dict[str, Any]], req: Dict[str, Any]
                              ) -> Tuple[List[Dict[str, Any]], int]:
    """Drop rows whose EMPLOYER is categorised and does not match.

    The filter was accepted, echoed in `applied_filters` and read by nothing —
    measured live, `company_categories: ["fintech"]` returned all 18,058
    matching postings. It runs here rather than in `build_query` because the
    categories live on `employers`, not on the posting, and Mongo cannot join.

    An employer the registry has not categorised is KEPT, not excluded: that is
    a gap in our records, not a statement about the company. The response says
    so — `unapplied_filters` reports this one as `partial`.
    """
    wanted = {str(c).lower() for c in (req.get("company_categories") or [])}
    if not wanted or not rows:
        return rows, 0
    cats = employer_categories_for(rows)
    if not cats:
        return rows, 0
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for row in rows:
        ten = str(row.get("tenant") or "").strip().lower()
        got: List[str] = []
        if ten:
            got = (cats.get(f"{str(row.get('ats') or '').strip().lower()}:{ten}")
                   or cats.get(f":{ten}") or [])
        if got and not (set(got) & wanted):
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def _collapse_location_variants(rows: List[Dict[str, Any]]
                                ) -> List[Dict[str, Any]]:
    """One card per role per employer, with the other cities kept on it.

    Distinct from `_dedupe`: these ARE different requisitions, they are just
    the same job in another city, and a feed that shows eleven of them shows
    one job. The cities are preserved on the survivor rather than dropped.
    """
    best: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in rows:
        key = str(row.get("variant_group_id") or "")
        if not key:
            key = f"_r{len(order)}"
        prev = best.get(key)
        if prev is None:
            best[key] = row
            order.append(key)
            continue
        variants = prev.setdefault("location_variants", [])
        locs = row.get("locations") or []
        raw = (locs[0].get("raw") if locs and isinstance(locs[0], dict) else None)
        if raw and len(variants) < 8:
            variants.append({"location": raw, "job_id": row.get("job_id"),
                             "url": row.get("canonical_url")})
        prev["duplicate_count"] = int(prev.get("duplicate_count") or 0) + 1
    return [best[k] for k in order]


#: How many postings one employer may hold in a single page before the rest of
#: its results are pushed to later pages. Ordering only - nothing is removed.
EMPLOYER_PAGE_CAP = int(os.getenv("TECH_FEED_EMPLOYER_PAGE_CAP", "3") or 3)


def _diversify_employers(rows: List[Dict[str, Any]], page_size: int
                         ) -> Tuple[List[Dict[str, Any]], int]:
    """Reorder so no employer monopolises a page. Returns `(rows, moved)`.

    Measured on the live feed for one account: 15 of the first 25 cards were
    JPMorgan Chase. They were not duplicates - Android, iOS, Java/AWS,
    React/Java, all different requisitions - and every one of them deserved
    its score. They just should not all be on page one, because a page that
    shows one employer fifteen times has shown the user one employer.

    This is ORDERING, not scoring: `match_score` is untouched, nothing is
    dropped, and an employer's fourth-best role simply lands on page two. It
    belongs here for the same reason source quality does - `rank_score` and
    `match_score` are deliberately separate, and preference signals live in
    the former.

    Stable: within an employer, relative order is preserved exactly.
    """
    cap = max(1, EMPLOYER_PAGE_CAP)
    size = max(1, int(page_size or DEFAULT_PAGE_SIZE))
    if len(rows) <= size:
        return rows, 0

    def _key(row: Dict[str, Any]) -> str:
        """`(ats, tenant)` first — the same key `employer_identity.
        apply_resolution` adjudicates on, and the only one that holds up.

        `employer_id` on a posting is a slug derived from whatever name the
        board printed, and one employer's board prints many. JPMorgan's Oracle
        instance serves three CX sites under tenant `jpmc` and stamps its
        postings `jpmorgan-chase`, `nutmeg-container-corporation` and
        `chase-family-limited-partnership-no-5` — legal entities, not
        employers. Keyed on the slug, those read as three companies and one of
        them took nine of the first twenty-five cards.
        """
        ats = str(row.get("ats") or "").strip().lower()
        tenant = str(row.get("tenant") or "").strip().lower()
        if tenant:
            return f"{ats}|{tenant}"
        return (str(row.get("employer_name") or "").strip().lower()
                or str(row.get("employer_id") or "").strip().lower()
                or "")

    pending = list(rows)
    out: List[Dict[str, Any]] = []
    moved = 0
    # Fill one page at a time: walk what is left in RANKED order and take the
    # first `size` rows that keep every employer at or under `cap`. Anything
    # skipped stays queued, in order, and gets first refusal on the next page.
    while pending:
        used: Dict[str, int] = {}
        page: List[Dict[str, Any]] = []
        held: List[Dict[str, Any]] = []
        for row in pending:
            if len(page) >= size:
                held.append(row)
                continue
            k = _key(row)
            if k and used.get(k, 0) >= cap:
                held.append(row)
                moved += 1
                continue
            used[k] = used.get(k, 0) + 1
            page.append(row)
        # Top up a short page from what was held back. The cap is best-effort,
        # not a quota: when the remaining results genuinely ARE one employer,
        # there is no diversity to be had, and emitting a four-row page would
        # only shift every later page boundary while showing the user less.
        if len(page) < size and held:
            take = size - len(page)
            page.extend(held[:take])
            moved -= min(take, moved)
            held = held[take:]
        if not page:
            out.extend(held)
            break
        out.extend(page)
        pending = held
    return out, moved


def search(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run one search, or serve a page of an existing one.

    A cursor short-circuits everything: no Mongo query beyond the session read
    and a `$in` fetch of one page of ids, no re-ranking, no re-scoring, no
    ingestion. That is the acceptance criterion "subsequent pages must not
    repeat ingestion or LLM work", enforced structurally rather than by
    convention.
    """
    started = time.perf_counter()
    req = normalize_request(payload)
    cursor = decode_cursor(payload.get("cursor"))
    if cursor:
        search_id, offset, keyset = cursor
        if keyset is not None:
            return _serve_keyset_page(search_id, keyset, req, started)
        return _serve_page(search_id, offset, req, started)
    return _run_search(req, started)


def _run_search(req: Dict[str, Any], started: float) -> Dict[str, Any]:
    now = _now()
    warnings: List[str] = []

    docs, diagnostics = retrieve(req, now=now)
    watermark = _watermark(docs)
    keyset = diagnostics.pop("_keyset", None)
    truncated = len(docs) >= CANDIDATE_LIMIT
    total_matched = int(diagnostics.get("total_matched") or 0)
    total_is_exact = bool(diagnostics.get("total_is_exact"))
    if truncated:
        # NOT "narrow the filters to reach the rest" any more — the rest IS
        # reachable. The ranked window is a scoring bound; paging past it
        # continues in index order, and the message now says which is which
        # instead of presenting our memory limit as the edge of the market.
        shown = f"{total_matched:,}" if total_is_exact else f"over {COUNT_LIMIT:,}"
        warnings.append(
            f"{shown} postings match. The first {CANDIDATE_LIMIT:,} are ranked "
            f"by relevance; paging beyond them continues in date order.")

    # The user's suppression set, read ONCE per search - not per page (a cursor
    # page is served from the order recorded here) and not per row. A guest has
    # no identity to key on, so there is no query at all.
    suppressed = None
    if req.get("user_email"):
        from services import tech_feed_feedback
        suppressed = tech_feed_feedback.suppressed_for(req["user_email"])

    t = time.perf_counter()
    kept, drops = hard_filter(docs, req, suppressed=suppressed)
    diagnostics["filter_ms"] = round((time.perf_counter() - t) * 1000, 1)
    diagnostics["filtered_out"] = dict(sorted(drops.items(), key=lambda kv: -kv[1]))
    diagnostics["after_filter"] = len(kept)
    # How much of the candidate set had to be classified at READ time because
    # `tech` is not backfilled yet. This is the number that should fall to 0
    # as the backfill runs, and it is the single largest CPU cost per search.
    diagnostics["read_time_classified"] = sum(
        1 for d in docs if not (isinstance(d.get("tech"), dict)
                                and d["tech"].get("taxonomy_version")))

    t = time.perf_counter()
    _resolve_employers(kept)
    kept, _cat_dropped = filter_company_categories(kept, req)
    if _cat_dropped:
        diagnostics["company_category_dropped"] = _cat_dropped
    diagnostics["employer_resolve_ms"] = round((time.perf_counter() - t) * 1000, 1)
    if req["employer_scope"] == EMPLOYER_SCOPE_TECH:
        kept, tech_only_dropped = _apply_employer_scope(kept)
        diagnostics["employer_scope_dropped"] = tech_only_dropped

    profile, skills, years = _load_profile(req)
    if req["personalize"] and req["user_email"] and profile is None:
        warnings.append(
            "No parsed resume found, so results are not personalized and "
            "carry no match score.")

    from services import inventory_feed
    kept = inventory_feed._dedupe(kept)
    kept = _collapse_location_variants(kept)

    # Phase two. Only the survivors, and only on a PERSONALIZED search - an
    # unpersonalized feed never reads `requirements` at all.
    #
    # Gated on the REQUEST, not on `profile`. It used to be gated on
    # `_load_profile` returning something, but `inventory_feed._score_rows`
    # builds its own profile when handed None - so the two could disagree, and
    # when they did every row was scored with NO requirements: the
    # `insufficient_job_requirements` cap pinned the whole page at 40 and
    # every hard constraint vanished, on a search that looked like it worked.
    if req["personalize"] and req["user_email"]:
        t_hydrate = time.perf_counter()
        diagnostics["hydrated"] = hydrate_requirements(kept)
        diagnostics["hydrate_ms"] = round(
            (time.perf_counter() - t_hydrate) * 1000, 1)

    t = time.perf_counter()
    rank(kept, req, profile, skills, years, now=now)
    kept, moved = _diversify_employers(kept, req["page_size"])
    diagnostics["employer_diversity_moved"] = moved
    diagnostics["rank_ms"] = round((time.perf_counter() - t) * 1000, 1)
    t = time.perf_counter()
    facets = build_facets(kept)
    diagnostics["facet_ms"] = round((time.perf_counter() - t) * 1000, 1)
    order = [str(r.get("job_id") or "") for r in kept if r.get("job_id")]

    search_id = uuid.uuid4().hex
    coverage = coverage_block()
    health = source_health_block()
    warnings.extend(health.get("warnings") or [])

    # Stamp the total BEFORE persisting. `tech_feed_metrics.search_metrics`
    # reads latency off the session document, and `_envelope` adds `total_ms`
    # after the save - so every recorded p50/p95 was 0.
    diagnostics["total_ms"] = round((time.perf_counter() - started) * 1000, 1)

    _save_session(search_id=search_id, req=req, order=order, facets=facets,
                  watermark=watermark, diagnostics=diagnostics,
                  coverage=coverage, source_health=health, warnings=warnings,
                  keyset=keyset if truncated else None,
                  total_matched=total_matched or len(order),
                  total_is_exact=total_is_exact)

    if not order:
        suggestions = broadening_suggestions(req, diagnostics.get("filtered_out") or {})
        diagnostics["broadening_suggestions"] = suggestions
        if suggestions:
            warnings.append(
                "No postings matched. " + "; ".join(
                    s["label"] for s in suggestions[:3]) + ".")
        else:
            warnings.append("No postings matched, and no filter is obviously "
                            "responsible - the inventory may not cover this "
                            "slice yet. See `coverage`.")

    page = kept[:req["page_size"]]
    more_in_order = len(order) > req["page_size"]
    # The ranked window is exhausted but the match set is not: hand straight
    # to the keyset phase rather than reporting the end of results.
    has_more = more_in_order or bool(truncated and keyset)
    if more_in_order:
        next_cursor = encode_cursor(search_id, req["page_size"])
    elif has_more:
        next_cursor = encode_cursor(search_id, keyset=keyset)
    else:
        next_cursor = None
    return _envelope(
        search_id=search_id, req=req,
        jobs=[_card(r, now=now) for r in page],
        next_cursor=next_cursor,
        has_more=has_more, total=int(total_matched or len(order)),
        facets=facets, watermark=watermark,
        coverage=coverage, source_health=health, warnings=warnings,
        diagnostics=diagnostics, started=started)


def _position_of(doc: Dict[str, Any]) -> Dict[str, Any]:
    """A document's position in `(posted_at desc, job_id asc)` order."""
    return {"p": _as_utc(doc.get("posted_at")), "j": str(doc.get("job_id") or "")}


def _index_position_key(doc: Dict[str, Any]) -> Tuple[int, float, str]:
    """Sort key matching `(posted_at desc, job_id asc)`, undated last."""
    posted = doc.get("posted_at")
    if isinstance(posted, datetime):
        return (0, -posted.timestamp(), str(doc.get("job_id") or ""))
    return (1, 0.0, str(doc.get("job_id") or ""))


def keyset_predicate(keyset: Dict[str, Any]) -> Dict[str, Any]:
    """The "everything strictly after this row" clause for `(posted_at desc,
    job_id asc)`.

    BSON orders `Null < Date`, so a descending sort puts UNDATED postings
    last, and `{"posted_at": {"$lt": <date>}}` already includes them. That is
    correct and load-bearing: 18,222 live postings carry no date, and a naive
    keyset would either skip them entirely or serve them twice. Once the
    traversal reaches them, `p` is None and the clause narrows to the undated
    tail ordered by `job_id`.
    """
    posted = keyset.get("p")
    job_id = str(keyset.get("j") or "")
    if posted is None:
        return {"posted_at": None, "job_id": {"$gt": job_id}}
    # The `$lte` clause is what makes this fast, and it is not redundant with
    # the `$or`. An `$or` alone cannot bound an index range: measured on live
    # `job_postings`, the deep-page query examined 71,333 index keys to return
    # 250 rows and took 13.6 seconds, because Mongo could only use the base
    # `posted_at >= cutoff` bound and evaluated the `$or` as a residual over
    # everything after it. Adding the upper bound on the SAME field gives one
    # closed range, and the range SHRINKS as paging advances - so later pages
    # get cheaper rather than more expensive.
    return {"$and": [
        {"posted_at": {"$lte": posted}},
        {"$or": [
            {"posted_at": {"$lt": posted}},
            {"posted_at": posted, "job_id": {"$gt": job_id}},
        ]},
    ]}


def _serve_keyset_page(search_id: str, keyset: Dict[str, Any],
                       req: Dict[str, Any], started: float) -> Dict[str, Any]:
    """Continue a search PAST the ranked window, by index position.

    This is the half of paging that has no ceiling. `_serve_page` replays a
    frozen, personalised order and therefore cannot exceed `MAX_ORDERED`;
    this reads forward from a recorded `(posted_at, job_id)` and can reach
    every matching indexed posting.

    What is preserved, and what honestly is not:

    * ORDERING is stable and total — index order, never re-shuffled, and the
      response says `sort: "index_order"` in its diagnostics so a client can
      tell it is no longer relevance-ranked. Ranking a page in isolation
      would produce an order that looks personalised and is not comparable
      across pages, which is worse than saying so.
    * DEDUPLICATION still runs (`inventory_feed._dedupe` plus location-variant
      collapsing), within the page.
    * CURSOR EXPIRY is unchanged: the session still carries the filters and
      still expires, and a dead session still returns 410.
    """
    session = _load_session(search_id, req.get("user_email") or "")
    if not session:
        return {"error": "cursor_expired",
                "message": "This result set has expired. Re-run the search.",
                "search_id": search_id, "jobs": [], "next_cursor": None,
                "has_more": False, "feed_version": FEED_VERSION}

    from services import inventory_feed
    from services import job_inventory_service as inv

    now = _now()
    stored_req = normalize_request(session.get("applied_filters") or req)
    page_size = req["page_size"]
    stored_req["page_size"] = page_size

    base = build_query(stored_req, now=now)
    query = {"$and": [base, keyset_predicate(keyset)]}
    projection = (_PROJECTION if _needs_requirements_upfront(stored_req)
                  else _PROJECTION_LIGHT)

    suppressed = None
    if stored_req.get("user_email"):
        from services import tech_feed_feedback
        suppressed = tech_feed_feedback.suppressed_for(stored_req["user_email"])

    profile, skills, years = _load_profile(stored_req)

    kept: List[Dict[str, Any]] = []
    scanned = 0
    cursor_key: Optional[Dict[str, Any]] = dict(keyset)
    exhausted = False
    t = time.perf_counter()

    # Read forward in bounded hops until the page is full. Each hop advances
    # `cursor_key` past every row EXAMINED, not merely every row kept, so a
    # heavily-filtered slice never re-reads what it already rejected.
    while len(kept) < page_size and scanned < KEYSET_MAX_SCAN:
        hop = min(page_size * KEYSET_OVERFETCH, KEYSET_MAX_SCAN - scanned)
        batch = list(inv._postings()
                     .find({"$and": [base, keyset_predicate(cursor_key)]}, projection)
                     .sort([("posted_at", -1), ("job_id", 1)]).limit(hop))
        if not batch:
            exhausted = True
            break
        scanned += len(batch)
        last = batch[-1]
        cursor_key = {"p": _as_utc(last.get("posted_at")),
                      "j": str(last.get("job_id") or "")}
        for doc in batch:
            doc["_tech"] = _read_time_tech(doc)
        survivors, _drops = hard_filter(batch, stored_req, suppressed=suppressed)
        kept.extend(survivors)
        if len(batch) < hop:
            exhausted = True
            break

    _resolve_employers(kept)
    kept, _ = filter_company_categories(kept, stored_req)
    if stored_req["employer_scope"] == EMPLOYER_SCOPE_TECH:
        kept, _dropped = _apply_employer_scope(kept)
    kept = inventory_feed._dedupe(kept)
    kept = _collapse_location_variants(kept)
    # Index order, explicitly, before slicing. Every stage above preserves
    # input order, but the cursor for the next page is derived from the LAST
    # ROW OF THIS PAGE, so "in order" has to be a guarantee rather than a
    # property that happens to hold.
    kept.sort(key=_index_position_key)
    page = kept[:page_size]

    if profile is not None:
        hydrate_requirements(page)
        rank(page, stored_req, profile, skills, years, now=now)
        # `rank` re-sorts by relevance; index order is what the cursor
        # promised, so restore it. The per-page scores are still attached and
        # still true - they simply do not reorder a page whose position in the
        # traversal is fixed.
        page.sort(key=_index_position_key)

    # A hop can over-fetch: 80 raw rows can survive into 40 kept when the page
    # is 10. Serving 10 and reporting "exhausted" threw the other 30 away and
    # ended the traversal 30 rows early. When there is a remainder, the next
    # cursor resumes from the last row SERVED; only when the whole hop was
    # consumed does it resume from the last row EXAMINED (which is faster,
    # because it skips rows already rejected).
    if len(kept) > len(page):
        cursor_key = _position_of(page[-1]) if page else cursor_key
        has_more = True
    else:
        has_more = not exhausted and bool(cursor_key)
    warnings = list(session.get("warnings") or [])
    total = session.get("total_matched") or session.get("total") or len(page)
    return _envelope(
        search_id=search_id, req=stored_req,
        jobs=[_card(d, now=now) for d in page],
        next_cursor=(encode_cursor(search_id, keyset=cursor_key) if has_more else None),
        has_more=has_more, total=int(total),
        facets=session.get("facets") or {},
        watermark=session.get("inventory_watermark"),
        coverage=session.get("coverage") or {},
        source_health=session.get("source_health") or {},
        warnings=warnings,
        diagnostics={"served_from": "keyset", "sort": "index_order",
                     "scanned": scanned, "kept": len(kept),
                     "exhausted": exhausted,
                     "keyset_ms": round((time.perf_counter() - t) * 1000, 1)},
        started=started)


def _sponsorship_of(row: Dict[str, Any]) -> Optional[str]:
    """`"not_offered"`, `"offered"` or None (the posting did not say).

    Only an EXPLICIT statement produces a value. Silence is by far the common
    case - most employers say nothing about sponsorship - and turning silence
    into "not offered" would remove from a visa-dependent candidate's feed
    every job that simply did not mention it.
    """
    reqs = row.get("requirements")
    if isinstance(reqs, dict) and reqs.get("work_authorization_restricted"):
        return "not_offered"
    blob = f"{row.get('title_raw') or ''} {row.get('description_text') or ''}"[:4000]
    if re.search(r"\b(visa sponsorship (?:is )?available|will sponsor|"
                 r"sponsorship (?:is )?provided|h-?1b sponsorship available)\b",
                 blob, re.IGNORECASE):
        return "offered"
    if re.search(r"\b(no (?:visa )?sponsorship|not able to sponsor|"
                 r"unable to sponsor|does not (?:offer|provide) sponsorship|"
                 r"without (?:current or future )?sponsorship)\b",
                 blob, re.IGNORECASE):
        return "not_offered"
    return None


def _clearance_of(row: Dict[str, Any]) -> Optional[bool]:
    """True when the posting states a clearance requirement, else None.

    Never False. "This posting did not mention a clearance" is not the same
    claim as "this posting does not require one", and only the first is
    supported by the evidence.
    """
    reqs = row.get("requirements")
    if isinstance(reqs, dict) and reqs.get("clearance_required"):
        return True
    if _mentions_clearance(str(row.get("title_raw") or "")):
        return True
    return None


def _serve_page(search_id: str, offset: int, req: Dict[str, Any],
                started: float) -> Dict[str, Any]:
    session = _load_session(search_id, req.get("user_email") or "")
    if not session:
        # An expired or foreign cursor is not an error the user caused. Say so
        # and let the client re-run the search rather than 500ing mid-scroll.
        return {"error": "cursor_expired",
                "message": "This result set has expired. Re-run the search.",
                "search_id": search_id, "jobs": [], "next_cursor": None,
                "has_more": False, "feed_version": FEED_VERSION}

    order: List[str] = list(session.get("order") or [])
    page_size = req["page_size"]
    slice_ids = order[offset:offset + page_size]
    if not slice_ids:
        session_keyset = session.get("keyset")
        if session_keyset:
            return _serve_keyset_page(search_id, session_keyset, req, started)
        return _envelope(
            search_id=search_id, req=session.get("applied_filters") or req,
            jobs=[], next_cursor=None, has_more=False,
            total=int(session.get("total_matched") or len(order)),
            facets=session.get("facets") or {},
            watermark=session.get("inventory_watermark"),
            coverage=session.get("coverage") or {},
            source_health=session.get("source_health") or {},
            warnings=session.get("warnings") or [],
            diagnostics={"served_from": "session"}, started=started)

    from services import job_inventory_service as inv
    docs = list(inv._postings().find({"job_id": {"$in": slice_ids}},
                                     _PROJECTION_LIGHT))
    by_id = {d.get("job_id"): d for d in docs}
    # Preserve the ORDER recorded at search time. A `$in` returns index order,
    # not argument order, and re-sorting here would silently reshuffle the
    # page relative to what the cursor promised.
    ordered = [by_id[i] for i in slice_ids if i in by_id]
    missing = len(slice_ids) - len(ordered)

    now = _now()
    stored_req = normalize_request(session.get("applied_filters") or req)
    stored_req["page_size"] = page_size
    for doc in ordered:
        doc["_tech"] = _read_time_tech(doc)
    _resolve_employers(ordered)
    profile, skills, years = _load_profile(stored_req)
    if profile is not None:
        hydrate_requirements(ordered)
    rank(ordered, stored_req, profile, skills, years, now=now)
    # `rank` re-sorts; the session order is authoritative, so restore it.
    pos = {jid: i for i, jid in enumerate(slice_ids)}
    ordered.sort(key=lambda d: pos.get(d.get("job_id"), 10 ** 6))

    warnings = list(session.get("warnings") or [])
    if missing:
        warnings.append(
            f"{missing} posting(s) on this page closed since the search ran.")

    next_offset = offset + page_size
    more_in_order = next_offset < len(order)
    session_keyset = session.get("keyset")
    if more_in_order:
        next_cursor = encode_cursor(search_id, next_offset)
        has_more = True
    elif session_keyset:
        # End of the ranked window, more matches beyond it. Switch cursor
        # shape rather than stopping — this is the join between the two
        # paging phases, and it is the single place the 2,000-row ceiling
        # used to live.
        next_cursor = encode_cursor(search_id, keyset=session_keyset)
        has_more = True
    else:
        next_cursor, has_more = None, False
    return _envelope(
        search_id=search_id, req=stored_req,
        jobs=[_card(d, now=now) for d in ordered],
        next_cursor=next_cursor,
        has_more=has_more,
        total=int(session.get("total_matched") or len(order)),
        facets=session.get("facets") or {},
        watermark=session.get("inventory_watermark"),
        coverage=session.get("coverage") or {},
        source_health=session.get("source_health") or {},
        warnings=warnings,
        diagnostics={"served_from": "session", "page_offset": offset},
        started=started)


def _company_docs_for(names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """`{name_key: companies document}` for the employers on this page.

    ONE `$in` query, not one per employer.

    The join is on `companies.name_normalized`, which strips legal suffixes but
    keeps case and keeps words that `employer_identity.name_key` removes
    ("UNITEDHEALTH GROUP" vs "unitedhealth"). Measured agreement between the
    two conventions is 87%, so BOTH spellings are sent and the result is keyed
    on `name_key` so the caller's lookup matches either way.
    """
    from services import employer_identity as ei

    wanted: Dict[str, str] = {}
    for n in names:
        key = ei.name_key(n)
        if key:
            wanted[key] = str(n or "")
    if not wanted:
        return {}
    candidates: Set[str] = set()
    for key, raw in wanted.items():
        candidates.add(key.upper())
        # their convention: strip legal suffixes only
        candidates.add(re.sub(r"[^A-Za-z0-9 ]+", " ",
                              str(raw).upper()).strip())
    try:
        docs = _db().companies.find(
            {"name_normalized": {"$in": sorted(c for c in candidates if c)[:400]}},
            {"_id": 0, "company_id": 1, "name": 1, "name_normalized": 1,
             "sic": 1, "sic_description": 1, "signals": 1, "cik": 1,
             "domain": 1, "tech": 1},
        ).limit(1000)
    except Exception as exc:  # pragma: no cover - the filter degrades, not fails
        logger.warning("tech_feed: company lookup failed (%s)", type(exc).__name__)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        for candidate in (doc.get("name"), doc.get("name_normalized")):
            k = ei.name_key(candidate)
            if k and k not in out:
                out[k] = doc
    return out


def _apply_employer_scope(rows: List[Dict[str, Any]]
                          ) -> Tuple[List[Dict[str, Any]], int]:
    """`employer_scope=tech_only`. Employer classification, NOT occupation.

    The registry is the primary signal and the candidate-set posting mix is
    only a supplement, because the mix ALONE cannot work here: measured on a
    live core-tech search, a 2,000-row candidate set contained 191 distinct
    employers of which **4** cleared `POSTING_MIX_MIN_ROWS`, median 1 row each.
    An earlier version of this function classified from the mix alone and
    therefore dropped nothing at all - a filter that renders in the UI, is
    echoed in `applied_filters`, and does nothing. That is worse than not
    having it.

    So the employers on the page are joined to `companies` in one query,
    which brings the SEC SIC code and any persisted verdict with it.

    An employer we cannot classify is KEPT. "Unknown" must not silently narrow
    the market, and the card reports `employer_tech_classification: "unknown"`
    so the user can see why it is there.
    """
    from services import employer_identity as ei
    from services import employer_tech_classifier as etc
    from collections import Counter

    mixes: Dict[str, Counter] = {}
    for row in rows:
        key = ei.name_key(row.get("employer_name")) or str(row.get("employer_id") or "")
        scope = (row.get("_tech") or {}).get("tech_scope") or "unknown"
        mixes.setdefault(key, Counter())[scope] += 1

    companies = _company_docs_for(
        {str(r.get("employer_name") or "") for r in rows})

    verdicts: Dict[str, Any] = {}
    for key, counter in mixes.items():
        company = companies.get(key)
        if company and isinstance(company.get("tech"), dict) \
                and company["tech"].get("tech_classification"):
            # A persisted verdict (scripts/tech_employer_coverage.py
            # --persist-classification) was computed with the employer's WHOLE
            # board, not this page's slice. Trust it over anything derivable
            # here.
            stored = company["tech"]
            verdicts[key] = etc.EmployerClassification(
                employer_name=str(company.get("name") or ""),
                tech_classification=str(stored.get("tech_classification")),
                tech_categories=list(stored.get("tech_categories") or []),
                confidence=float(stored.get("confidence") or 0.0),
                evidence=list(stored.get("evidence") or []),
                evidence_source=str(stored.get("evidence_source") or "persisted"),
            )
            continue
        verdicts[key] = etc.classify_employer(
            company or {"name": key}, posting_mix=dict(counter))

    kept: List[Dict[str, Any]] = []
    dropped = 0
    for row in rows:
        key = ei.name_key(row.get("employer_name")) or str(row.get("employer_id") or "")
        v = verdicts.get(key)
        cls = v.tech_classification if v else etc.TECH_UNKNOWN
        row["_employer_tech"] = cls
        row["_employer_tech_evidence"] = (v.evidence if v else [])[:3]
        if cls == etc.TECH_NON:
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def broadening_suggestions(req: Dict[str, Any], drops: Dict[str, int]
                           ) -> List[Dict[str, Any]]:
    """One-click ways out of a zero-result search, ranked by what actually cut.

    Derived from `filtered_out`, so the suggestions name the filters that
    really removed rows rather than guessing. Each carries the exact patch a
    client applies, so the UI does not have to re-derive the inverse of a
    filter it did not write.

    Returned on the RESULT, not left to the frontend: the backend is the only
    party that knows which stage dropped what, and two clients would otherwise
    each invent their own (differing) answer.
    """
    out: List[Dict[str, Any]] = []

    def add(label: str, patch: Dict[str, Any], removed: int = 0) -> None:
        out.append({"label": label, "patch": patch, "removed": removed})

    if req["posted_within_days"] < 90:
        add(f"Search the last 90 days instead of {req['posted_within_days']}",
            {"posted_within_days": 90})
    if drops.get("salary_below_floor") or drops.get("salary_unknown"):
        n = (drops.get("salary_below_floor", 0) + drops.get("salary_unknown", 0))
        if not req["include_unknown_salary"]:
            add("Include jobs without a published salary",
                {"include_unknown_salary": True}, n)
        if req["min_salary"]:
            add(f"Remove the ${int(req['min_salary']):,} salary floor",
                {"min_salary": None}, n)
    if drops.get("tech_scope") and req["tech_scope"] == ["core"]:
        add("Include adjacent technical roles (TPM, solutions engineering, "
            "IT, silicon design)",
            {"tech_scope": ["core", "adjacent"]}, drops["tech_scope"])
    # Suggested whenever the filter is SET, not only when it shows in `drops`.
    # A specialty that matches nothing is applied in Mongo, so those rows never
    # reach `hard_filter` and never appear in the drop counts - the filter most
    # likely to have emptied the page is exactly the one with no evidence here.
    if req["role_specialties"]:
        add("Search the whole role family, not just the specialty",
            {"role_specialties": []}, drops.get("role_specialty", 0))
    if req["role_families"]:
        add("Search every technical role family",
            {"role_families": []}, drops.get("role_family", 0))
    if drops.get("title_mismatch"):
        add("Drop the exact-title requirement",
            {"titles": [], "query": ""}, drops["title_mismatch"])
    if drops.get("missing_required_skill"):
        add("Treat required skills as preferred",
            {"skills_preferred": list(req["skills_required"]),
             "skills_required": []}, drops["missing_required_skill"])
    if req["work_modes"] and "unknown" not in req["work_modes"]:
        # 96.1% of live postings do not state a work arrangement. A remote
        # filter that excludes `unknown` is excluding almost the whole market.
        add("Include jobs that do not state a work arrangement",
            {"work_modes": req["work_modes"] + ["unknown"]})
    if req["employer_scope"] == EMPLOYER_SCOPE_TECH:
        add("Include technical roles at non-technology employers",
            {"employer_scope": EMPLOYER_SCOPE_ANY},
            drops.get("employer_scope_dropped", 0))
    if req["employer_kind"] != EMPLOYER_KIND_ANY:
        add("Include every employer type",
            {"employer_kind": EMPLOYER_KIND_ANY}, drops.get("employer_kind", 0))
    if req["cities"] or req["regions"]:
        add("Search the whole country", {"cities": [], "regions": []})
    if req["apply_mode"] == APPLY_MODE_DIRECT:
        add("Include postings that are not direct-apply",
            {"apply_mode": APPLY_MODE_ANY}, drops.get("not_direct_apply", 0))
    if req["min_years"] is not None or req["max_years"] is not None:
        add("Remove the experience range",
            {"min_years": None, "max_years": None})

    # Most-effective first, then the order above (which is roughly
    # least-destructive first) for ties.
    out.sort(key=lambda s: -int(s.get("removed") or 0))
    return out[:6]


#: Request fields that `normalize_request` accepts and echoes but that no
#: stage actually applies. Each needs data the inventory does not carry yet;
#: none of them is a bug to be quietly tolerated, and listing them is how the
#: list gets shorter.
INERT_FILTERS: Dict[str, str] = {
    "radius_km": "needs geocoded coordinates; postings carry place names only",
    "remote_eligible_locations": ("needs a posting to state WHICH geographies "
                                  "its remote role is open to; 96.1% state no "
                                  "work mode at all"),
    "timezone_overlap_hours": "needs a stated timezone on the posting",
    "work_authorization": ("needs the posting's accepted statuses; only an "
                           "explicit no-sponsorship statement is parsed today"),
    "resume_id": "the scorer uses every parsed resume and reports the best",
}


#: `status` values on an `unapplied_filters` entry.
FILTER_NOT_APPLIED = "not_applied"   #: nothing narrowed on it at all
FILTER_PARTIAL = "partial"           #: narrows only the rows carrying the data


def unapplied_filters(req: Dict[str, Any]) -> List[Dict[str, str]]:
    """The filters this request set that did not do what the name implies.

    Two different claims, and conflating them would replace one lie with
    another:

    * `not_applied` — nothing narrowed on it. A client that renders
      `applied_filters` as "Server applied" is telling the user a filter
      worked when it did not, which is worse than not offering it: they
      believe they have excluded something and stop looking.
    * `partial` — it DID narrow, but only the rows that carry the data it
      tests; rows without it are kept rather than excluded. Showing that as
      "not applied" would be the opposite error.
    """
    out: List[Dict[str, str]] = []
    for key, reason in INERT_FILTERS.items():
        value = req.get(key)
        if value in (None, "", [], 0):
            continue
        out.append({"filter": key, "reason": reason,
                    "status": FILTER_NOT_APPLIED})
    # PARTIAL filters. These DO narrow, but only the rows that carry the data
    # they test, and rows without it are kept rather than excluded - the
    # unknown-is-not-a-no rule this codebase applies everywhere. Reporting
    # them here is a weaker and more accurate claim than the applied list's.
    if str(req.get("sponsorship_preference") or "any") != "any":
        out.append({"filter": "sponsorship_preference",
                    "reason": ("applied only to postings that state their "
                               "sponsorship position; most do not, and those "
                               "are kept rather than excluded"),
                    "status": FILTER_PARTIAL})
    if req.get("company_categories"):
        out.append({"filter": "company_categories",
                    "reason": ("applied only to employers the registry has "
                               "categorised; uncategorised employers are kept"),
                    "status": FILTER_PARTIAL})
    if req.get("employment_types"):
        out.append({"filter": "employment_types",
                    "reason": ("applied to postings whose employment type we "
                               "have normalised; the rest are kept, not "
                               "excluded"),
                    "status": FILTER_PARTIAL})
    if req.get("skills_required"):
        out.append({"filter": "skills_required",
                    "reason": ("applied to postings whose skills we could "
                               "extract; a posting we could not parse is kept "
                               "rather than failed on a test it never got"),
                    "status": FILTER_PARTIAL})
    if req.get("min_years") is not None or req.get("max_years") is not None:
        out.append({"filter": "min_years/max_years",
                    "reason": ("matched against the experience the posting "
                               "STATES; 76.4% state none and those are kept"),
                    "status": FILTER_PARTIAL})
    if req.get("query"):
        out.append({"filter": "query",
                    "reason": ("searches the title, employer, role taxonomy "
                               "and extracted skills - not the full job "
                               "description, which is not held inline"),
                    "status": FILTER_PARTIAL})
    return out


def _envelope(*, search_id: str, req: Dict[str, Any], jobs: List[Dict[str, Any]],
              next_cursor: Optional[str], has_more: bool, total: int,
              facets: Dict[str, Any], watermark: Optional[datetime],
              coverage: Dict[str, Any], source_health: Dict[str, Any],
              warnings: List[str], diagnostics: Dict[str, Any],
              started: float) -> Dict[str, Any]:
    from services import fit_score
    from services import tech_taxonomy as tx
    diagnostics = dict(diagnostics)
    diagnostics["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return {
        "search_id": search_id,
        "jobs": jobs,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "total": total,
        "facets": facets,
        "applied_filters": req,
        # `applied_filters` echoes what was REQUESTED. Some of it is not
        # applied, and a UI that renders the echo as "Server applied" tells
        # the user their filter narrowed the results when it did not. This
        # names those, so the gap is visible rather than convincing.
        "unapplied_filters": unapplied_filters(req),
        "taxonomy_version": tx.TAXONOMY_VERSION,
        "fit_score_version": getattr(fit_score, "SCORE_VERSION", "fit-v2.0"),
        "feed_version": FEED_VERSION,
        "inventory_watermark": _as_utc(watermark),
        "coverage": coverage,
        "source_health": source_health,
        "warnings": warnings,
        "broadening_suggestions": diagnostics.get("broadening_suggestions") or [],
        "diagnostics": diagnostics,
    }


def taxonomy_catalog() -> Dict[str, Any]:
    """The filter tree the UI renders. Static; safe to cache hard."""
    from services import tech_taxonomy as tx
    return tx.taxonomy_catalog()


def ensure_indexes() -> List[str]:
    """Indexes this module's reads depend on. Idempotent; safe to re-run."""
    from pymongo import ASCENDING, DESCENDING
    created: List[str] = []
    db = _db()
    specs = [
        (db[SESSIONS_COLLECTION], [("search_id", ASCENDING)],
         {"name": "search_id_unique", "unique": True}),
        (db[SESSIONS_COLLECTION], [("expire_at", ASCENDING)],
         {"name": "session_ttl", "expireAfterSeconds": 0}),
        (db.job_postings, [("status", ASCENDING), ("tech.tech_scope", ASCENDING),
                           ("posted_at", DESCENDING)],
         {"name": "active_tech_scope_posted_at"}),
        (db.job_postings, [("status", ASCENDING), ("tech.role_families", ASCENDING),
                           ("posted_at", DESCENDING)],
         {"name": "active_tech_family_posted_at"}),
        (db.job_postings, [("status", ASCENDING),
                           ("tech.role_specialties", ASCENDING),
                           ("posted_at", DESCENDING)],
         {"name": "active_tech_specialty_posted_at"}),
        (db.job_postings, [("status", ASCENDING), ("dedupe_group_id", ASCENDING)],
         {"name": "active_dedupe_group"}),
        (db.job_postings, [("status", ASCENDING), ("variant_group_id", ASCENDING)],
         {"name": "active_variant_group"}),
        (db.job_postings, [("status", ASCENDING), ("tenant", ASCENDING),
                           ("posted_at", DESCENDING)],
         {"name": "active_tenant_posted_at"}),
    ]
    for coll, keys, opts in specs:
        try:
            created.append(coll.create_index(keys, **opts))
        except Exception as exc:  # pragma: no cover
            logger.warning("tech_feed: index %s failed (%s)",
                           opts.get("name"), type(exc).__name__)
    return created
