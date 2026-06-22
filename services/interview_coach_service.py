"""Interview Pattern Coach + ATS/fit/salary trifecta (Pillar D).

The unique angle: we persist EVERY mock-evaluation as an `interview_attempts`
row (the existing mock-evaluate route used to score-and-discard), then aggregate
ACROSS roles to surface recurring weak spots one-off prep tools never see —
"you lose points on specificity/metrics in 7 of 9 answers" — and recommend
targeted drills.

All reads aggregate deterministically (cheap); the narrative + fit + salary use
the cheap FLASH tier with small caches.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from utils.db_connect import DBConnect
from services.gemini_client import gemini_json, GEMINI_FLASH

logger = logging.getLogger(__name__)

DIMENSIONS = ["relevance", "clarity", "specificity", "structure", "technical_accuracy"]
_SENIORITY_WORDS = {"senior", "junior", "staff", "principal", "lead", "sr", "jr",
                    "i", "ii", "iii", "iv", "entry", "mid", "level", "associate"}


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _iso(v):
    if not hasattr(v, "isoformat"):
        return v
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


def normalize_role_family(title: str) -> str:
    t = (title or "").lower().replace(".", " ")
    words = [w for w in t.replace("/", " ").split() if w not in _SENIORITY_WORDS]
    return " ".join(words).strip() or "general"


# ---------------------------------------------------------------------------
# Capture (called from the mock-evaluate route)
# ---------------------------------------------------------------------------
def record_attempt(user_email: str, record_id: str, role: str, category: str,
                   question: str, user_answer: str, evaluation: Dict) -> str:
    """Persist one evaluated answer. Tolerant of missing fields."""
    dims = evaluation.get("dimensions") or {}
    dims = {d: int(dims.get(d)) for d in DIMENSIONS if isinstance(dims.get(d), (int, float))}
    doc = {
        "attempt_id": str(uuid.uuid4()),
        "user_email": user_email,
        "record_id": record_id,
        "role": role,
        "role_family": normalize_role_family(role),
        "category": (category or "behavioral").lower(),
        "question": question[:1500],
        "question_hash": hashlib.sha1((question or "").encode("utf-8")).hexdigest()[:16],
        "user_answer": (user_answer or "")[:4000],
        "score": int(evaluation.get("score") or 0),
        "dimensions": dims,
        "improvements": (evaluation.get("improvements") or [])[:6],
        "missing_points": (evaluation.get("missing_points") or [])[:6],
        "weak_spot_tags": [str(t)[:60] for t in (evaluation.get("weak_spot_tags") or [])[:8]],
        "answered_at": _now(),
    }
    try:
        _db().interview_attempts.insert_one(doc)
    except Exception as e:
        logger.warning("record_attempt failed for %s: %s", user_email, e)
    return doc["attempt_id"]


# ---------------------------------------------------------------------------
# Reads / aggregation
# ---------------------------------------------------------------------------
def get_attempts(user_email: str, limit: int = 50) -> List[Dict]:
    docs = list(_db().interview_attempts.find({"user_email": user_email})
                .sort("answered_at", -1).limit(limit))
    out = []
    for d in docs:
        d.pop("_id", None)
        d["answered_at"] = _iso(d.get("answered_at"))
        d.pop("user_answer", None)  # keep the list response light
        out.append(d)
    return out


def _aggregate(attempts: List[Dict]) -> Dict:
    n = len(attempts)
    if not n:
        return {"count": 0}
    avg_score = round(sum(a.get("score", 0) for a in attempts) / n)
    dim_totals: Dict[str, List[int]] = {d: [] for d in DIMENSIONS}
    tag_counts: Dict[str, int] = {}
    cat_scores: Dict[str, List[int]] = {}
    for a in attempts:
        for d in DIMENSIONS:
            v = (a.get("dimensions") or {}).get(d)
            if isinstance(v, (int, float)):
                dim_totals[d].append(int(v))
        for t in a.get("weak_spot_tags") or []:
            key = t.strip().lower()
            tag_counts[key] = tag_counts.get(key, 0) + 1
        cat_scores.setdefault(a.get("category", "behavioral"), []).append(a.get("score", 0))
    dim_avg = {d: (round(sum(v) / len(v)) if v else None) for d, v in dim_totals.items()}
    weak_dims = sorted([d for d, v in dim_avg.items() if v is not None],
                       key=lambda d: dim_avg[d])[:2]
    top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:5]
    return {
        "count": n,
        "avg_score": avg_score,
        "dimension_avg": dim_avg,
        "weakest_dimensions": weak_dims,
        "top_weak_spots": [{"tag": t, "count": c} for t, c in top_tags],
        "by_category": {k: round(sum(v) / len(v)) for k, v in cat_scores.items()},
    }


def coach_summary(user_email: str, full: bool = False, predictive: bool = False) -> Dict:
    """Cross-role weak-spot analysis + a short coaching narrative.

    Free tier passes full=False → last 5 attempts, no trend. Premium passes
    full=True → entire history + score trend. Premium+ predictive=True adds
    targeted drill recommendations.
    """
    limit = 0 if full else 5
    cur = _db().interview_attempts.find({"user_email": user_email}).sort("answered_at", -1)
    if limit:
        cur = cur.limit(limit)
    attempts = list(cur)
    stats = _aggregate(attempts)
    if not stats.get("count"):
        return {"stats": stats, "narrative": None, "drills": [], "trend": []}

    trend = []
    if full:
        for a in sorted(attempts, key=lambda x: x.get("answered_at") or _now()):
            trend.append({"at": _iso(a.get("answered_at")), "score": a.get("score", 0),
                          "category": a.get("category")})

    narrative = None
    drills: List[Dict] = []
    try:
        wd = ", ".join(stats.get("weakest_dimensions") or []) or "n/a"
        tags = ", ".join(t["tag"] for t in stats.get("top_weak_spots", [])) or "none yet"
        prompt = (
            "You are a candid interview coach. Based on this candidate's aggregated mock-interview "
            "performance, write a SHORT analysis (3-4 sentences) of their recurring weakness pattern "
            "across roles, then "
            + ("recommend 3 specific practice drills (each: title + one-line why). " if predictive else "give 1 focused tip. ")
            + "Be specific and encouraging.\n\n"
            f"Attempts: {stats['count']}, avg score: {stats['avg_score']}\n"
            f"Weakest dimensions: {wd}\n"
            f"Most frequent weak-spot tags: {tags}\n"
            f"By category: {stats.get('by_category')}\n\n"
            'Return strict JSON: {"narrative": "...", "drills": [{"title": "...", "why": "..."}]}'
        )
        res = gemini_json(prompt, max_tokens=900, temperature=0.4, model=GEMINI_FLASH,
                          schema={"narrative": str, "drills": list})
        narrative = (res or {}).get("narrative")
        if predictive:
            drills = (res or {}).get("drills") or []
    except Exception as e:
        logger.warning("coach narrative failed: %s", e)

    return {"stats": stats, "narrative": narrative, "drills": drills, "trend": trend}


# ---------------------------------------------------------------------------
# Interview-fit (one role)
# ---------------------------------------------------------------------------
def interview_fit(user_email: str, record_id: str) -> Dict:
    db = _db()
    record = db.tailoring_records.find_one({"record_id": record_id, "user_email": user_email})
    if not record:
        raise ValueError("Record not found")
    jd = record.get("jd_analysis") or {}
    weak = _aggregate(list(db.interview_attempts.find({"user_email": user_email}).sort("answered_at", -1).limit(20)))
    prompt = (
        "Assess how ready this candidate is to interview for the role, factoring in their resume fit "
        "AND their known interview weak spots. Be concrete and honest.\n\n"
        f"Role: {jd.get('job_title')} at {jd.get('company')}\n"
        f"Required skills: {', '.join((jd.get('required_skills') or [])[:20])}\n"
        f"Candidate's recurring interview weak spots: {', '.join(weak.get('weakest_dimensions') or []) or 'none recorded'}; "
        f"tags: {', '.join(t['tag'] for t in weak.get('top_weak_spots', [])) or 'none'}\n\n"
        'Return strict JSON: {"fit_score": 0-100, "verdict": "one sentence", '
        '"focus_areas": ["2-4 things to prep"], "likely_questions": ["3-5 questions they should rehearse"]}'
    )
    res = gemini_json(prompt, max_tokens=1200, temperature=0.4, model=GEMINI_FLASH,
                      schema={"fit_score": int, "verdict": str, "focus_areas": list, "likely_questions": list})
    return res or {}


# ---------------------------------------------------------------------------
# Salary benchmark (cached AI estimate)
# ---------------------------------------------------------------------------
def salary_benchmark(role: str, level: str = "", location: str = "",
                     with_percentiles: bool = False) -> Dict:
    role_family = normalize_role_family(role)
    bench_key = hashlib.sha1(f"{role_family}|{level}|{location}".lower().encode()).hexdigest()[:20]
    db = _db()
    cached = db.salary_benchmarks.find_one({"bench_key": bench_key})
    if cached and cached.get("expires_at") and _aware(cached["expires_at"]) > _now():
        cached.pop("_id", None)
        cached["expires_at"] = _iso(cached.get("expires_at"))
        return cached

    prompt = (
        "Give a realistic 2025-2026 total-compensation estimate for this role. Base it on general market "
        "knowledge; clearly an estimate, not a guarantee.\n\n"
        f"Role: {role}\nLevel: {level or 'unspecified'}\nLocation: {location or 'US (remote-friendly)'}\n\n"
        'Return strict JSON: {"currency": "USD", "low": int, "median": int, "high": int, '
        + ('"percentiles": {"p25": int, "p50": int, "p75": int, "p90": int}, ' if with_percentiles else "")
        + '"confidence": "low|medium|high", "notes": "one sentence"}'
    )
    schema = {"currency": str, "low": int, "median": int, "high": int, "confidence": str, "notes": str}
    if with_percentiles:
        schema["percentiles"] = dict
    res = gemini_json(prompt, max_tokens=600, temperature=0.3, model=GEMINI_FLASH, schema=schema) or {}

    doc = {
        "bench_key": bench_key, "role_family": role_family, "level": level, "location": location,
        "estimate": res, "basis": "ai_estimate", "confidence": res.get("confidence"),
        "created_at": _now(), "expires_at": _now() + timedelta(days=30),
    }
    try:
        db.salary_benchmarks.update_one({"bench_key": bench_key}, {"$set": doc}, upsert=True)
    except Exception as e:
        logger.warning("salary cache write failed: %s", e)
    doc["expires_at"] = _iso(doc["expires_at"])
    doc.pop("created_at", None)
    return doc


def _aware(dt):
    return dt if (dt is None or dt.tzinfo) else dt.replace(tzinfo=timezone.utc)
