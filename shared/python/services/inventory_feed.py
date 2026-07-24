"""The authenticated feed tabs, served from the canonical inventory.

`workday_jobs_service.run_workday_jobs_search` and
`career_pages_service.run_career_pages_search` each answer one tab by crawling
their whole catalog live: 1013 Workday tenants crossed with up to 6 query terms,
and 1309 career-page companies, inside the user's request, against a 210s
budget. That is the same fan-out ADR-003 removed from the guest path.

This module answers those tabs from `job_postings` instead. It is deliberately
a SEPARATE module rather than an edit inside those two functions:

* the legacy path stays byte-for-byte intact as a fallback, so a cold shard or
  a bad deploy is one env var away from the old behaviour;
* the payload contract is asserted in one place instead of two;
* scoring moves onto the shared engine (below) without having to unpick the
  two legacy scorers first.

Scoring
-------
Rows are scored with `fit_score`, the ADR-002 engine that already serves the
guest path, using the requirement profile the crawler extracted. The legacy
`_score_jobs` in each service cannot be reused here: it reads `description`,
and inventory rows deliberately carry no JD text (bodies live in S3). Using
`fit_score` is also what ADR-002 asked for - "a single versioned engine, shared
by guest and authenticated paths" - so selection and display stop disagreeing.

The tab's own presentation helpers (`_attach_feasibility`, sponsorship,
window counts) are reused as-is, so what changes is where candidates come from
and how they are scored, not how they are rendered.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

SOURCE_WORKDAY = "workday"
SOURCE_CAREER = "career-pages"

# How many postings the ranking stage considers. Scoring is a few microseconds
# per row off a precomputed profile, so this is chosen for recall.
CANDIDATE_LIMIT = int(os.getenv("FEED_INVENTORY_CANDIDATES", "1500") or 1500)
# Below this the inventory is treated as not yet warm for the requested slice
# and the caller falls back to the live crawl. A thin tab is a worse answer
# than a slow one.
MIN_ROWS = int(os.getenv("FEED_INVENTORY_MIN_ROWS", "25") or 25)
# The authenticated tabs are browse surfaces, not the guest freshness promise
# (ADR-001 keeps the strict 7-day gate for guests only).
WINDOW_DAYS = int(os.getenv("FEED_INVENTORY_WINDOW_DAYS", "30") or 30)
GLOBAL_KEEP = 1500


def enabled() -> bool:
    """`FEED_MATCH_MODE=scan` is the rollback to the legacy live crawl."""
    return (os.getenv("FEED_MATCH_MODE") or "inventory").strip().lower() != "scan"


# ---------------------------------------------------------------------------
# Inventory doc -> tab row
# ---------------------------------------------------------------------------

_ATS_LABEL = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters", "workable": "Workable",
    "recruitee": "Recruitee", "workday": "Workday",
}


def _row_from_posting(doc: Dict[str, Any], *, tz_offset: int) -> Optional[Dict[str, Any]]:
    """One `job_postings` document in the shape the tab payload already uses.

    Field-for-field compatible with `career_pages_service._row` and the Workday
    tab's row builder, so the frontend renders it unchanged.
    """
    from services.workday_jobs_service import _days_ago

    posted_at = doc.get("posted_at")
    if not isinstance(posted_at, datetime):
        return None
    aware = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
    iso = aware.isoformat()
    days = _days_ago(iso, tz_offset)
    if days is None:
        return None

    locations = doc.get("locations") or []
    first = locations[0] if isinstance(locations, list) and locations else {}
    location = str(first.get("raw") or "") if isinstance(first, dict) else str(first)
    country = str(first.get("country") or "") if isinstance(first, dict) else ""
    work_mode = doc.get("work_mode") or ""
    ats = doc.get("ats") or ""
    url = str(doc.get("canonical_url") or "")

    return {
        # The canonical inventory id, so a tab row can be tailored against and
        # pinned by the same reference the guest path and applications use.
        "job_id": doc.get("job_id"),
        "title": str(doc.get("title_raw") or "")[:200],
        "company": str(doc.get("employer_name") or ""),
        "tenant": doc.get("tenant") or "",
        "ats": ats,
        "industry": doc.get("industry") or "other",
        "location": location or "Not specified",
        "country_hint": country.lower(),
        "country_code": "US" if country.lower() in ("us", "usa", "united states") else None,
        "date_posted": iso,
        "days_ago": days,
        "posted_text": _posted_text(days),
        "apply_link": url,
        "url": url,
        "description": "",          # body lives in S3; scoring uses requirements
        "salary": "",
        "employment_type": doc.get("employment_type") or "",
        "is_remote": work_mode == "remote",
        "h1b_sponsor": False,
        "contract_friendly": False,
        "source": _ATS_LABEL.get(ats, "Workday" if doc.get("source") == SOURCE_WORKDAY else "Career Page"),
        "seniority": doc.get("seniority") or "",
        "_requirements": doc.get("requirements") or None,
    }


def _posted_text(days: Optional[int]) -> str:
    if days is None:
        return ""
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"


# ---------------------------------------------------------------------------
# Retrieval + scoring
# ---------------------------------------------------------------------------

def _role_families(titles: Iterable[str], skills: Iterable[str]) -> List[str]:
    """Families to retrieve for.

    Errs wide, exactly as the guest path does: retrieval that is too tight
    returns nothing and reads as an empty market, while the scorer is good at
    pushing a poor fit down. An explicit title filter from the tab UI takes
    priority; with no titles at all we return [] and retrieve unfiltered, which
    is the correct reading of "show me everything".
    """
    from services import fit_score

    out: List[str] = []
    for value in list(titles or []):
        fam = fit_score.classify_role(str(value or ""))
        if fam and fam not in out:
            out.append(fam)
    return out


def _score_rows(rows: List[Dict[str, Any]], user_email: str,
                resume_skills: List[str], resume_years: Optional[int]) -> None:
    """Score in place with the shared engine.

    Writes `match_score` / `matched_skills` / `missing_skills` - the three
    fields the tab UI and `_rank` already consume - plus the versioned `fit`
    block the guest cards carry, so both surfaces explain a score the same way.
    """
    from services import fit_score

    # Same resolution path `fit_score.score_batch_for_user` uses:
    # users.active_resume_id -> resume_assets (Resume Platform v3).
    resume: Dict[str, Any] = {}
    if user_email:
        try:
            from services import resume_access

            record = resume_access.get_active_structured(user_email)
            resume = (record or {}).get("structured") or {}
        except Exception as exc:
            logger.warning("inventory_feed: could not load resume for scoring: %s",
                           type(exc).__name__)
            resume = {}

    if resume:
        profile = fit_score.build_resume_profile(resume)
    elif resume_skills:
        profile = fit_score._profile_from_skills(resume_skills, None, resume_years)
    else:
        # No resume and no skills: there is nothing to score against, so leave
        # every row unscored rather than invent a number. `_rank` then falls
        # back to recency, which is the honest ordering.
        for row in rows:
            row["match_score"] = 0
            row["matched_skills"] = []
            row.setdefault("missing_skills", [])
        return

    for row in rows:
        verdict = fit_score.score_one(
            jd_text="", title=row.get("title") or "",
            profile=profile, location=row.get("location") or "",
            job={"requirements": row.get("_requirements")},
        )
        row["match_score"] = verdict["match_score"]
        row["matched_skills"] = verdict.get("matched_skills") or []
        row["missing_skills"] = (verdict.get("missing_top") or [])[:5]
        row["fit"] = {
            "score_version": verdict.get("score_version"),
            "band": verdict.get("band"),
            "confidence": verdict.get("confidence"),
            "components": verdict.get("component_scores") or {},
            "explanation": str(verdict.get("explanation") or "")[:400],
            "missing_requirements": (verdict.get("missing_requirements") or [])[:4],
        }


def _dedupe(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse the same opening listed more than once.

    Employers routinely publish one requisition per location, and large ones
    re-post the same role across sub-boards, so an undeduped feed shows the same
    job three times in the top five. `(company, title, location)` is the same
    key the guest merge uses; the freshest copy wins so the displayed age stays
    honest.
    """
    best: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str, str]] = []
    for row in rows:
        key = (
            str(row.get("company") or "").strip().lower(),
            str(row.get("title") or "").strip().lower(),
            str(row.get("location") or "").strip().lower(),
        )
        prev = best.get(key)
        if prev is None:
            best[key] = row
            order.append(key)
            continue
        prev_age = prev.get("days_ago")
        new_age = row.get("days_ago")
        if new_age is not None and (prev_age is None or new_age < prev_age):
            best[key] = row
    return [best[k] for k in order]


def _window_counts(jobs: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"today": 0, "d1": 0, "d3": 0, "d7": 0, "d30": 0}
    for job in jobs:
        days = job.get("days_ago")
        if days is None:
            continue
        if days == 0:
            counts["today"] += 1
        for key, limit in (("d1", 1), ("d3", 3), ("d7", 7), ("d30", 30)):
            if days <= limit:
                counts[key] += 1
    return counts


def build_feed(source: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Serve one feed tab from inventory, or None when it cannot.

    Returns None (rather than a thin payload) when the requested slice has too
    few postings, so the caller falls back to the live crawl. That is the only
    honest behaviour while shards are still warming: an empty Workday tab would
    read as "there are no jobs", which is false.
    """
    from services import job_inventory_service as inv
    from services.workday_jobs_service import (
        _attach_feasibility, _attach_sponsorship, _clamp_tz_offset,
        _load_resume_profile,
    )

    started = datetime.now(timezone.utc)
    titles = [str(t).strip() for t in (payload.get("titles") or []) if str(t or "").strip()]
    industries = [str(i).strip() for i in (payload.get("industries") or []) if str(i or "").strip()]
    companies = [str(c).strip() for c in (payload.get("companies") or []) if str(c or "").strip()]
    us_only = bool(payload.get("us_only", True))
    remote_only = bool(payload.get("remote_only", False))
    user_email = str(payload.get("user_email") or "")
    tz_offset = _clamp_tz_offset(payload.get("tz_offset_minutes"))

    skills, resume_years = _load_resume_profile(user_email)
    if not user_email:
        payload_skills = [str(s).strip() for s in (payload.get("resume_skills") or [])
                          if str(s or "").strip()]
        if payload_skills:
            skills = payload_skills[:60]
        try:
            if payload.get("resume_years") is not None:
                resume_years = max(0, int(payload["resume_years"]))
        except (TypeError, ValueError):
            pass

    families = _role_families(titles, skills)
    try:
        docs = inv.find_active_fresh(
            source=source,
            role_families=families or None,
            industries=industries or None,
            tenants=companies or None,
            max_age_days=WINDOW_DAYS,
            work_mode="remote" if remote_only else None,
            limit=CANDIDATE_LIMIT,
        )
    except Exception as exc:
        logger.warning("inventory_feed: retrieval failed for %s: %s",
                       source, type(exc).__name__)
        return None

    # Reuse the tab's OWN geo filter rather than a country-code check. Most
    # inventory rows carry no parsed country (boards ship a free-text location),
    # so a country-code test passes almost everything and leaks obvious non-US
    # rows - "IN_Senior Associate_DevOps_GCC" and Commonwealth Bank both showed
    # up in a US-only slice. `_keep_us` does the tiered location-text analysis
    # the legacy path already relies on.
    from services.career_pages_service import _keep_us

    rows: List[Dict[str, Any]] = []
    for doc in docs:
        row = _row_from_posting(doc, tz_offset=tz_offset)
        if row is None:
            continue
        if us_only and not _keep_us(row):
            continue
        rows.append(row)
    rows = _dedupe(rows)

    if len(rows) < MIN_ROWS:
        logger.info("inventory_feed: %s slice too thin (%d < %d) - deferring to crawl",
                    source, len(rows), MIN_ROWS)
        return None

    _score_rows(rows, user_email, skills, resume_years)
    rows.sort(key=lambda j: (-(j.get("match_score") or 0),
                            j.get("days_ago") if j.get("days_ago") is not None else 99))
    rows = rows[:GLOBAL_KEEP]

    _attach_feasibility(rows, skills, resume_years)
    rows.sort(key=lambda j: (-(j.get("match_score") or 0),
                             j.get("days_ago") if j.get("days_ago") is not None else 99))
    try:
        _attach_sponsorship(rows)
    except Exception as exc:  # pragma: no cover - enrichment is best effort
        logger.warning("inventory_feed: sponsorship skipped: %s", type(exc).__name__)

    tenants_with_results = len({r.get("tenant") for r in rows if r.get("tenant")})
    for row in rows:
        row.pop("_requirements", None)

    result: Dict[str, Any] = {
        "ok": True,
        "generated_at": started.isoformat(),
        "jobs": rows,
        "total": len(rows),
        "window_counts": _window_counts(rows),
        "query_terms": titles,
        "tenants_total": _registry_size(source),
        # The crawl already covered the whole registry; there is no per-request
        # partial progress to report, and claiming otherwise would be a lie the
        # tab's progress UI would render.
        "tenants_done": _registry_size(source),
        "tenants_with_results": tenants_with_results,
        "industries_available": _industries_for(source),
        "cache_hit": True,
        "us_only": us_only,
        "resume_skill_count": len(skills),
        "diagnostics": {"task_errors": 0, "served_from": "inventory",
                        "candidates": len(docs)},
        "errors": [],
        "served_from": "inventory",
    }
    if source == SOURCE_CAREER:
        from services.career_pages_catalog import ATS_KINDS

        result["ats_available"] = ATS_KINDS
    return result


def _registry_size(source: str) -> int:
    try:
        from services import job_ingest_worker as w

        return len(w.registry_members(source))
    except Exception:  # pragma: no cover
        return 0


def _industries_for(source: str) -> List[str]:
    try:
        if source == SOURCE_CAREER:
            from services.career_pages_catalog import INDUSTRIES

            return list(INDUSTRIES)
        from services.workday_jobs_service import INDUSTRIES as WD_INDUSTRIES

        return list(WD_INDUSTRIES)
    except Exception:  # pragma: no cover
        return []
