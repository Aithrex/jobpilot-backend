"""MNCs / Big-Tech search — third variant of the career-pages engine.

Fans out across the curated MNC_CATALOG (mnc_catalog.py) with one task per
company, dispatching per engine:

  career_pages - board ATS JSON APIs (reuses career_pages_service.FETCHERS)
  enterprise   - Amazon / Eightfold / Phenom search APIs (reuses
                 career_pages_service.ENTERPRISE_FETCHERS)
  workday      - the public Workday CXS endpoint (reuses
                 workday_cxs_fetcher._fetch_tenant; NVIDIA-style configs)
  aggregator   - JSearch "<title> <company>" for bot-walled companies
                 (Google/Meta/Apple/Tesla/...) with a STRICT employer-name
                 match so only that company's postings pass; skipped
                 gracefully (and reported in diagnostics) without a key.

Everything downstream — resume profile, query/skill terms, US filter,
relevance gate, deterministic scoring, ranking, streaming partials, caching —
is the SAME pipeline as run_career_pages_search, so the result renders in the
existing WorkdayJobsPanel unchanged. No LLM anywhere.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.career_pages_service import (
    ENTERPRISE_FETCHERS,
    FETCHERS,
    _apply_career_sponsorship,
    _drain_pools,
    _enrich_details,
    _keep_relevant,
    _keep_us,
    _posted_text,
    _read_cache,
    _write_cache,
)
from services.mnc_catalog import MNC_CATALOG, MNC_ENGINES, MNC_SECTORS
from services.workday_cxs_fetcher import _fetch_tenant
from services.workday_jobs_service import (
    GLOBAL_KEEP,
    WINDOW_DAYS,
    _IN_LAMBDA,
    _attach_feasibility,
    _attach_sponsorship,
    _build_match_regex,
    _days_ago,
    _derive_query_terms,
    _derive_skill_terms,
    _env_int,
    _load_resume_profile,
    _role_matchers,
    _score_jobs,
)

logger = logging.getLogger(__name__)

# ~69 companies (vs career-pages' ~624) — a smaller pool finishes well inside
# the resume Lambda's 300s budget even with the per-term enterprise/workday
# fan-in. Env-overridable, optional (defaults always work).
OVERALL_TIMEOUT = _env_int("MNC_OVERALL_TIMEOUT", 180 if _IN_LAMBDA else 420)
MAX_WORKERS = _env_int("MNC_MAX_WORKERS", 16 if _IN_LAMBDA else 32)
PER_COMPANY_KEEP = 30
DETAIL_ENRICH_TOP = 150
DETAIL_ENRICH_RECENT = 150
STREAM_EVERY_TASKS = 8
STREAM_TOP = 120
MAX_QUERY_TERMS = 4
TOTAL_TERMS_CAP = 6
WORKDAY_TERM_CAP = 3      # per-term fan-in, like the enterprise fetchers
AGGREGATOR_TERM_CAP = 2   # JSearch calls cost RapidAPI credits — keep tight
WORKDAY_PER_TERM_LIMIT = 20
RESULT_SCHEMA_VERSION = 1

CACHE_COLLECTION = "mnc_pages_cache"
CACHE_TTL_SECONDS = 6 * 3600
CACHE_FRESH_SECONDS = 3 * 3600


# ----------------------------------------------------------------------
# Catalog helpers
# ----------------------------------------------------------------------

def _source_kind(row: Dict[str, Any]) -> str:
    """The source label used as `ats` in the catalog summary and cache key."""
    engine = row.get("engine")
    if engine == "career_pages":
        return row["ats"]
    if engine == "enterprise":
        return row["fetcher"]
    if engine == "workday":
        return "workday"
    return "aggregator"


def _company_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt an MNC row to the company dict the career-pages fetchers expect."""
    return {
        "display_name": row["display_name"],
        "ats": row["ats"] if row.get("engine") == "career_pages" else row.get("fetcher", ""),
        "slug": row["slug"],
        "industry": row["sector"],
        "host": row.get("host", ""),
        "extra": row.get("extra") or {},
    }


# ----------------------------------------------------------------------
# Per-engine fetchers (module-level names so tests can monkeypatch them)
# ----------------------------------------------------------------------

def _fetch_board(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    return FETCHERS[row["ats"]](_company_dict(row))


def _fetch_enterprise(row: Dict[str, Any], terms: List[str]) -> List[Dict[str, Any]]:
    jobs = ENTERPRISE_FETCHERS[row["fetcher"]](_company_dict(row), terms)
    # Same posting often matches several terms — dedupe here.
    return list({j["job_id"]: j for j in jobs}.values())


def _normalize_workday(row: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    days = _days_ago(raw.get("date_posted") or "")
    return {
        "job_id": raw.get("job_id") or f"workday:{row['slug']}:{raw.get('apply_link', '')[-24:]}",
        "title": str(raw.get("title") or "").strip()[:200],
        "company": row["display_name"],
        "tenant": row["slug"],
        "ats": "workday",
        "industry": row["sector"],
        "location": str(raw.get("location") or "").strip()[:160],
        "country_hint": "",
        "date_posted": raw.get("date_posted") or "",
        "days_ago": days,
        "posted_text": raw.get("posted_text") or _posted_text(days),
        "apply_link": raw.get("apply_link") or "",
        "description": "",
        "is_remote": bool(raw.get("is_remote")),
        "source": "Workday",
    }


def _fetch_workday(row: Dict[str, Any], terms: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for term in (terms[:WORKDAY_TERM_CAP] or [""]):
        out.extend(
            _fetch_tenant(
                row["tenant"], row["wd_host"], row["site"], row["display_name"],
                term, "", limit=WORKDAY_PER_TERM_LIMIT,
            )
        )
    normalized = [_normalize_workday(row, j) for j in out]
    return list({j["job_id"]: j for j in normalized}.values())


# Legal-form / generic suffix tokens stripped from the END of employer names
# before the strict match ("Google LLC" -> "google", "Uber Technologies,
# Inc." -> "uber"). Both sides of the comparison run through the same
# normalizer, so display names lose their suffixes identically.
_NAME_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "llc", "llp", "ltd",
    "limited", "co", "company", "plc", "se", "ag", "sa", "nv", "gmbh",
    "group", "holdings", "technologies", "usa", "us",
})
_NAME_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def _normalize_employer(name: str) -> str:
    toks = [t for t in _NAME_CLEAN_RE.sub(" ", str(name or "").lower()).split() if t]
    while toks and toks[0] == "the":
        toks.pop(0)
    while toks and toks[-1] in _NAME_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def _employer_allowed(row: Dict[str, Any], employer_name: str) -> bool:
    """STRICT employer gate for aggregator rows — exact normalized-name match
    only, so "Google Operations Center" / "Apple Bank" never ride in on a
    "Google" / "Apple" search."""
    norm = _normalize_employer(employer_name)
    if not norm:
        return False
    allowed = {_normalize_employer(row["display_name"])}
    allowed.update(_normalize_employer(a) for a in row.get("aliases") or [])
    allowed.discard("")
    return norm in allowed


def _normalize_aggregator(row: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    date_posted = raw.get("date_posted") or ""
    days = _days_ago(date_posted)
    return {
        "job_id": f"aggregator:{row['slug']}:{raw.get('job_id') or ''}",
        "title": str(raw.get("title") or "").strip()[:200],
        "company": row["display_name"],
        "tenant": row["slug"],
        "ats": "aggregator",
        "industry": row["sector"],
        "location": str(raw.get("location") or "").strip()[:160],
        "country_hint": "us",   # JSearch queried with country=us
        "date_posted": date_posted,
        "days_ago": days,
        "posted_text": _posted_text(days) or str(raw.get("posted_text") or ""),
        "apply_link": raw.get("apply_link") or "",
        "description": str(raw.get("description") or "")[:5000],
        "is_remote": bool(raw.get("is_remote")),
        "source": "JSearch",
    }


def _fetch_aggregator(row: Dict[str, Any], terms: List[str]) -> List[Dict[str, Any]]:
    from services.job_service import get_job_service
    svc = get_job_service()
    out: List[Dict[str, Any]] = []
    for term in terms[:AGGREGATOR_TERM_CAP]:
        raw = svc._search_jsearch_raw(
            query=f"{term} {row['display_name']}",
            location="United States",
            date_posted="month",   # aligned with the 30d WINDOW_DAYS window
            remote_only=False,
            employment_type="",
        )
        for j in raw:
            if not _employer_allowed(row, j.get("company") or ""):
                continue
            out.append(_normalize_aggregator(row, j))
    return list({j["job_id"]: j for j in out}.values())


def _jsearch_configured() -> bool:
    try:
        from services.job_service import JobService
        return bool(JobService._get_jsearch_key())
    except Exception:
        return False


# ----------------------------------------------------------------------
# Cache (same two-tier pattern as career-pages, own collection)
# ----------------------------------------------------------------------

def _get_cache_collection():
    try:
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        coll = db[CACHE_COLLECTION]
        try:
            # Named explicitly so scripts/migrate_indexes.py can re-assert the
            # same specs on a fresh DB without a name conflict.
            coll.create_index("cached_at", expireAfterSeconds=CACHE_TTL_SECONDS,
                              name="mnc_pages_cache_ttl")
            coll.create_index("cache_key")
        except Exception:
            pass
        return coll
    except Exception as e:
        logger.warning("mnc-jobs: cache unavailable: %s", e)
        return None


def _cache_key(rows: List[Dict[str, Any]], terms: List[str], us_only: bool) -> str:
    basis = json.dumps({
        "v": RESULT_SCHEMA_VERSION,
        "slugs": sorted(f"{_source_kind(r)}:{r['slug']}" for r in rows),
        "terms": sorted(t.lower() for t in terms),
        "us": bool(us_only),
    }, sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def run_mnc_search(
    payload: Dict[str, Any],
    partial_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Execute one MNCs search (async job runner entry, job_type mnc_jobs).

    payload mirrors the Career Pages tab: titles, industries (sector keys),
    companies (slugs), location (unused server-side beyond US filter),
    remote_only, us_only, force_refresh, user_email.
    """
    titles = [str(t).strip() for t in (payload.get("titles") or []) if str(t or "").strip()]
    industries = {str(i).strip() for i in (payload.get("industries") or []) if str(i or "").strip()}
    companies = {str(x).strip().lower() for x in (payload.get("companies") or []) if str(x or "").strip()}
    remote_only = bool(payload.get("remote_only", False))
    us_only = bool(payload.get("us_only", True))
    force_refresh = bool(payload.get("force_refresh", False))
    user_email = str(payload.get("user_email") or "")

    rows = [
        r for r in MNC_CATALOG
        if r.get("engine") in MNC_ENGINES
        and (not industries or r.get("sector") in industries)
        and (not companies or r["slug"].lower() in companies)
    ]
    diagnostics: Dict[str, Any] = {"task_errors": 0}
    if not _jsearch_configured():
        skipped = sorted(r["slug"] for r in rows if r["engine"] == "aggregator")
        if skipped:
            diagnostics["aggregator_skipped"] = skipped
            logger.info("mnc-jobs: JSEARCH_API_KEY not configured; skipping %d "
                        "aggregator companies", len(skipped))
        rows = [r for r in rows if r["engine"] != "aggregator"]

    skills, resume_years = _load_resume_profile(user_email)
    title_terms = _derive_query_terms(titles, cap=MAX_QUERY_TERMS)
    skill_terms = _derive_skill_terms(skills, title_terms,
                                      cap=TOTAL_TERMS_CAP - len(title_terms))
    terms = title_terms + skill_terms
    term_res = [re.compile(re.escape(t), re.IGNORECASE) for t in title_terms]
    # Broaden title matching: individual role tokens too ("devops engineer"
    # must keep "Senior DevOps & Platform Engineer").
    for t in title_terms:
        toks = [w for w in t.split() if len(w) > 3]
        if len(toks) >= 2:
            term_res.append(re.compile(
                r".*".join(re.escape(w) for w in toks), re.IGNORECASE))
    skill_res = [rx for rx in (_build_match_regex(s) for s in skills[:30]) if rx]

    errors: List[str] = []
    coll = _get_cache_collection()
    key = _cache_key(rows, terms, us_only)
    cache_hit = False
    raw_jobs: Optional[List[Dict[str, Any]]] = None
    if not force_refresh:
        raw_jobs = _read_cache(coll, key)
        cache_hit = raw_jobs is not None

    companies_total = len(rows)
    companies_done = 0
    companies_with_results = 0

    if raw_jobs is None:
        collected: List[Dict[str, Any]] = []
        done = 0
        hits: set = set()
        matchers = _role_matchers(titles)
        stream_rx = [(s, _build_match_regex(s)) for s in skills[:30]]
        prov_cache: Dict[str, Tuple[int, List[str]]] = {}

        def _provisional(j: Dict[str, Any]) -> Tuple[int, List[str]]:
            cached = prov_cache.get(j["job_id"])
            if cached is None:
                blob = (j["title"] + "\n" + (j.get("description") or "")[:1500]).lower()
                tl = j["title"].lower()
                tm = any(all(tok in tl for tok in m) for m in matchers)
                matched = [s for s, rx in stream_rx if rx is not None and rx.search(blob)]
                score = min(100, min(55, len(matched) * 6) + (45 if tm else (20 if matched else 0)))
                if not skills and not tm:
                    score = 15
                cached = (score, matched[:8])
                prov_cache[j["job_id"]] = cached
            return cached

        def _snapshot() -> None:
            if not partial_cb:
                return
            rows_snap = list({j["job_id"]: j for j in collected}.values())
            for j in rows_snap:
                sc, mt = _provisional(j)
                j["match_score"] = sc
                j["matched_skills"] = mt
                j.setdefault("missing_skills", [])
            rows_snap.sort(key=lambda x: (
                -x["match_score"],
                x.get("days_ago") if x.get("days_ago") is not None else 99))
            try:
                partial_cb({
                    "progress": {"tenants_done": done,
                                 "tenants_total": companies_total,
                                 "jobs_found": len(rows_snap)},
                    "jobs": [
                        {k: v for k, v in j.items() if k != "description"}
                        for j in rows_snap[:STREAM_TOP]
                    ],
                    "total": len(rows_snap),
                    "streaming": True,
                })
            except Exception:
                pass

        def _one_company(r: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
            try:
                engine = r["engine"]
                if engine == "career_pages":
                    jobs_c = _fetch_board(r)
                elif engine == "enterprise":
                    jobs_c = _fetch_enterprise(r, title_terms)
                elif engine == "workday":
                    jobs_c = _fetch_workday(r, title_terms)
                else:
                    jobs_c = _fetch_aggregator(r, title_terms)
            except Exception as e:
                return [], f"{r['slug']}: {type(e).__name__}"
            jobs_c = [j for j in jobs_c
                      if j.get("days_ago") is not None and j["days_ago"] <= WINDOW_DAYS]
            if us_only:
                jobs_c = [j for j in jobs_c if _keep_us(j)]
            jobs_c = _keep_relevant(jobs_c, term_res, skill_res)
            jobs_c.sort(key=lambda j: (j.get("_tier", 1), j.get("days_ago") or 0))
            jobs_c = jobs_c[:PER_COMPANY_KEEP]
            for j in jobs_c:
                j.pop("_tier", None)
                j["sector"] = r["sector"]
            return jobs_c, None

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_one_company, r): r for r in rows}
            try:
                for fut in as_completed(futures, timeout=OVERALL_TIMEOUT):
                    r = futures[fut]
                    done += 1
                    try:
                        items, err = fut.result()
                    except Exception as e:  # pragma: no cover
                        items, err = [], str(e)
                    if err:
                        diagnostics["task_errors"] += 1
                    if items:
                        hits.add(r["slug"])
                        collected.extend(items)
                    if partial_cb and done % STREAM_EVERY_TASKS == 0:
                        _snapshot()
            except TimeoutError:
                pending = sum(1 for f in futures if not f.done())
                errors.append(f"timeout: {pending} companies did not finish")
                for f in futures:
                    if not f.done():
                        f.cancel()
        companies_done = done
        companies_with_results = len(hits)
        raw_jobs = list({j["job_id"]: j for j in collected}.values())
        _write_cache(coll, key, raw_jobs)
    else:
        companies_done = companies_total
        companies_with_results = len({j.get("tenant") for j in raw_jobs})

    # Serve path: recompute recency, apply window + remote filters.
    jobs = []
    for j0 in raw_jobs:
        j = dict(j0)
        days = _days_ago(j.get("date_posted") or "")
        if days is None or days > WINDOW_DAYS:
            continue
        j["days_ago"] = days
        j["posted_text"] = _posted_text(days)
        jobs.append(j)
    if remote_only:
        jobs = [j for j in jobs if j.get("is_remote")]

    def _rank(items: List[Dict[str, Any]]) -> None:
        items.sort(key=lambda j: (
            -(j.get("match_score") or 0),
            j.get("days_ago") if j.get("days_ago") is not None else 99))

    jobs = _score_jobs(jobs, titles, skills, skill_terms)
    _rank(jobs)

    if partial_cb:
        try:
            partial_cb({
                "progress": {"tenants_done": companies_total,
                             "tenants_total": companies_total,
                             "jobs_found": len(jobs), "phase": "details"},
                "jobs": [{k: v for k, v in j.items() if k != "description"}
                         for j in jobs[:STREAM_TOP]],
                "total": len(jobs), "streaming": True,
            })
        except Exception:
            pass

    top = jobs[:DETAIL_ENRICH_TOP] + [
        j for j in jobs[DETAIL_ENRICH_TOP:]
        if (j.get("days_ago") if j.get("days_ago") is not None else 99) <= 3
    ][:DETAIL_ENRICH_RECENT]
    if _enrich_details(top):
        _score_jobs(top, titles, skills, skill_terms)
        _rank(jobs)
        if coll is not None:
            by_id = {j["job_id"]: j for j in top if j.get("description")}
            for rj in raw_jobs:
                e = by_id.get(rj["job_id"])
                if e is not None:
                    rj["description"] = e["description"]
            _write_cache(coll, key, raw_jobs)

    jobs = jobs[:GLOBAL_KEEP]
    _attach_feasibility(jobs, skills, resume_years)
    _rank(jobs)
    _attach_sponsorship(jobs)
    _apply_career_sponsorship(jobs)
    _drain_pools()

    window_counts = {"today": 0, "d1": 0, "d3": 0, "d7": 0, "d30": 0}
    for j in jobs:
        d = j.get("days_ago")
        if d is None:
            continue
        if d == 0:
            window_counts["today"] += 1
        if d <= 1:
            window_counts["d1"] += 1
        if d <= 3:
            window_counts["d3"] += 1
        if d <= 7:
            window_counts["d7"] += 1
        if d <= 30:
            window_counts["d30"] += 1

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": jobs,
        "total": len(jobs),
        "window_counts": window_counts,
        "query_terms": terms,
        "tenants_total": companies_total,
        "tenants_done": companies_done,
        "tenants_with_results": companies_with_results,
        "industries_available": [s for s in MNC_SECTORS
                                 if any(r["sector"] == s for r in MNC_CATALOG)],
        "ats_available": sorted({_source_kind(r) for r in MNC_CATALOG}),
        "cache_hit": cache_hit,
        "us_only": us_only,
        "resume_skill_count": len(skills),
        "diagnostics": diagnostics,
        "errors": errors[:20],
    }


def catalog_summary() -> Dict[str, Any]:
    by_sector: Dict[str, List[Dict[str, Any]]] = {}
    for r in MNC_CATALOG:
        by_sector.setdefault(r["sector"], []).append(r)
    return {
        "total": len(MNC_CATALOG),
        "industries": [
            {"key": k, "count": len(by_sector[k]),
             "companies": [{"name": r["display_name"], "tenant": r["slug"],
                            "ats": _source_kind(r)} for r in
                           sorted(by_sector[k], key=lambda x: x["display_name"].lower())]}
            for k in MNC_SECTORS if k in by_sector
        ],
    }
