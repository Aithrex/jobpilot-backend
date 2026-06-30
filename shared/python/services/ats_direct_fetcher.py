"""
Direct ATS fetcher for the live search path.

Wraps the public Greenhouse / Lever / Ashby endpoints already used by the
daily pipeline (services/daily_pipeline_service.py) and converts their output
to the live-search job shape. No Apify involvement, no auth required — every
fetch is a single GET against a public board JSON.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _to_search_shape(pipeline_row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a daily_pipeline ATS row into the live-search response shape."""
    source = (pipeline_row.get("source") or "ATS").lower()
    company = pipeline_row.get("company") or ""
    title = pipeline_row.get("title") or ""
    url = pipeline_row.get("url") or ""
    location = pipeline_row.get("location") or "Not specified"
    description = pipeline_row.get("description") or ""
    posted = pipeline_row.get("posted") or ""
    job_id_seed = f"{source}|{company}|{title}|{url}".lower()
    job_id = f"{source}_direct:" + hashlib.md5(job_id_seed.encode()).hexdigest()[:24]
    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "logo": "",
        "location": location,
        "apply_link": url,
        "description": description,
        "salary": "",
        "employment_type": "",
        "date_posted": posted,
        "posted_text": "",
        "is_remote": "remote" in location.lower(),
        "h1b_sponsor": False,  # re-checked downstream
        "contract_friendly": False,
        "source": f"{(pipeline_row.get('source') or 'ATS').strip()} Direct",
    }


_QUERY_TOKEN_RE = re.compile(r"[^a-z0-9+#.]+")


def _query_match(row: Dict[str, Any], query: str) -> bool:
    """Cheap keyword filter so we don't return every job at every company.

    The Greenhouse / Lever / Ashby helpers pull the entire board for a company
    (their APIs don't accept a query string). For a "cloud engineer" search
    that means we'd otherwise return marketing / sales / nurse roles too. We
    keep rows where at least one query token (length > 2) is in title or the
    first 600 chars of description.
    """
    if not query:
        return True
    tokens = [t for t in _QUERY_TOKEN_RE.split(query.lower()) if len(t) > 2]
    if not tokens:
        return True
    stop = {"the", "and", "for", "with", "new", "grad", "entry", "level", "h1b", "sponsor"}
    tokens = [t for t in tokens if t not in stop] or tokens
    haystack = (
        (row.get("title") or "").lower()
        + " "
        + (row.get("description") or "")[:600].lower()
    )
    return any(t in haystack for t in tokens)


def fetch_ats_direct(
    query: str = "",
    greenhouse: Optional[List[Tuple[str, str, str]]] = None,
    lever: Optional[List[Tuple[str, str, str]]] = None,
    ashby: Optional[List[Tuple[str, str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Fan out to public ATS boards in parallel and return normalized jobs."""
    try:
        from services.daily_pipeline_service import _scrape_ats_direct
    except Exception as e:
        logger.warning("ATS direct unavailable: %s", e)
        return []
    rows = _scrape_ats_direct(greenhouse=greenhouse, lever=lever, ashby=ashby)
    out = [_to_search_shape(r) for r in rows if _query_match(r, query)]
    logger.info("[ats-direct] returning %d/%d postings after query filter", len(out), len(rows))
    return out
