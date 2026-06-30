"""
JD Match Scorer — deterministic resume-vs-JD overlap scoring.

Reuses JD text the pipeline already scraped (no extra fetches, no LLM calls)
to compute a fast per-job match score against the user's stored resume.

Public API: score_batch(user_email, items) -> Dict[id, ScoreResult]

This is intentionally cheap so the frontend can fire one batch call per
pipeline run (50-100 items) without burning LLM quota or adding latency.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from schemas.resume_schemas import flatten_skills

logger = logging.getLogger(__name__)


# Word-boundary skill match — alphanumerics + dots/pluses (so "Node.js" / "C++"
# don't collapse). Skills shorter than 3 chars fall back to substring match
# (e.g. "Go", "R") because word-boundary fails on single tokens with no
# meaningful left/right context.
_NONWORD_BEFORE = re.compile(r"(?<![A-Za-z0-9])")
_NONWORD_AFTER = re.compile(r"(?![A-Za-z0-9])")


def _normalize_skill(s: str) -> str:
    return (s or "").strip().lower()


def _build_match_regex(skill_lower: str) -> Optional[re.Pattern]:
    """Compile a word-boundary regex for a single skill token.

    Returns None for empty / pathological inputs so the caller can skip cheaply.
    """
    if not skill_lower:
        return None
    if len(skill_lower) <= 2:
        # "Go" / "R" / "C" — too short for word-boundary; require surrounding
        # whitespace or punctuation to avoid matching "Google", "RuntimeError".
        return re.compile(r"(?:^|[\s.,;:/()\[\]{}])" + re.escape(skill_lower) + r"(?:$|[\s.,;:/()\[\]{}])",
                          re.IGNORECASE)
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(skill_lower) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _load_user_skills(user_email: str) -> List[str]:
    """Pull the user's most recent parsed resume, return deduped lowercase skills."""
    try:
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        resume = db.user_resumes.find_one(
            {"user_email": user_email, "structured": {"$exists": True}},
            sort=[("parsed_at", -1)],
        )
        if not resume:
            return []
        structured = resume.get("structured") or {}
        skills_raw = flatten_skills(structured) or []
        out: List[str] = []
        seen = set()
        for s in skills_raw:
            n = _normalize_skill(str(s))
            if not n or n in seen:
                continue
            seen.add(n)
            out.append(n)
        return out
    except Exception as e:
        logger.warning("jd_match_scorer: failed to load resume skills for %s: %s", user_email, e)
        return []


def score_one(
    jd_text: str,
    title: str,
    resume_skills: List[str],
    *,
    min_resume_skills: int = 8,
) -> Dict[str, Any]:
    """Score a single JD against a precompiled skill list.

    Returns dict with `match_score` (0..100), `matched_skills` (list[str]),
    `missing_top` (best-effort list[str]). Deterministic, no AI.

    Scoring is `matched / max(min_resume_skills, total_skills) * 100`. The
    `min_resume_skills` floor prevents a 3-skill resume from getting 100%
    for any JD that mentions all three — small resumes need a wider denom.
    """
    if not resume_skills:
        return {"match_score": 0, "matched_skills": [], "missing_top": [],
                "explain": "no-resume-skills"}

    blob = ((title or "") + "\n" + (jd_text or "")).lower()
    if not blob.strip():
        return {"match_score": 0, "matched_skills": [], "missing_top": resume_skills[:5],
                "explain": "empty-jd"}

    matched: List[str] = []
    missing: List[str] = []
    for sk in resume_skills:
        rx = _build_match_regex(sk)
        if rx is None:
            continue
        if rx.search(blob):
            matched.append(sk)
        else:
            missing.append(sk)

    denom = max(min_resume_skills, len(resume_skills))
    raw = (len(matched) / denom) * 100.0
    score = max(0, min(100, round(raw)))
    # Truncate matched list — the UI needs 8 names max, more is noise. Order
    # is the resume's original skill order (most-prominent first per parser).
    return {
        "match_score": score,
        "matched_skills": matched[:12],
        "missing_top": missing[:8],
        "explain": f"{len(matched)}/{denom}",
    }


def score_batch(
    user_email: str,
    items: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Score a batch of (id, title, description) tuples for a single user.

    Returns (scores_by_id, meta). meta carries the resume_skill_count and
    whether the user has any parsed skills (frontend uses this to decide
    whether to show the badge column at all).
    """
    skills = _load_user_skills(user_email)
    scores: Dict[str, Dict[str, Any]] = {}
    if not skills:
        return scores, {"resume_skill_count": 0, "has_resume": False}
    for it in items or []:
        jid = str(it.get("id") or it.get("job_id") or it.get("url") or "")
        if not jid:
            continue
        scores[jid] = score_one(
            jd_text=str(it.get("description") or ""),
            title=str(it.get("title") or ""),
            resume_skills=skills,
        )
    return scores, {"resume_skill_count": len(skills), "has_resume": True}
