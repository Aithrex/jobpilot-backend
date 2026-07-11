"""Cross-user market TREND signal + outcome-grounded learning (the data moat).

Where `differentiation_service` answers *"what could THIS user build to stand out
in THEIR own matched-JD market"*, this service adds the one thing a single user's
JDs can't give you: **what the broader market wants for a role right now**, learned
privately across the whole corpus of matched JDs — then intersected with the user's
own real gaps and their real application OUTCOMES (Gmail-detected rejections/ghosts).

Two responsibilities:

  1. `recompute_market_stats()` — a nightly, cross-user aggregation. Over every
     matched JD in the corpus (`saved_jobs` + `tailoring_records`, ALL users), it
     buckets skills by NORMALIZED ROLE (`_role_family`) and TIME WINDOW (trailing
     30 / 90 days) and RECOMPUTES skill frequency IN CODE — an LLM number is never
     trusted for a countable fact. The output collection `skill_market_stats`
     stores ONLY aggregate counts: `{role, skill, window, count, pct, updated_at}`.
     No `user_email`, no JD text, no per-user row ever lands there — the aggregate
     is privacy-safe by construction (counts across many users, k-anonymity floor
     via `MIN_SKILL_COUNT` / `MIN_ROLE_JDS`).

  2. `trending_for_user(user_email)` — ranks candidate learning topics by
        market demand  ×  user gap  ×  learnability
     up-weighted by OUTCOME PRESSURE (a skill that keeps showing up in the JDs the
     user got rejected/ghosted on is a louder signal than one that never cost them
     anything). It reuses `differentiation_service`'s CODE-side reject filter to
     drop certifications, course-completions, soft skills and baseline skills, then
     caps the list. Each survivor links straight to a `learning_service` plan and
     the saved jobs that learning it would unlock.

This service is DETERMINISTIC — no LLM call, so no fabrication surface and no
per-request spend. The generative step (turning a topic into a plan) is the
already-entitlement-gated `POST /learning/plan`; a trend item just carries the
`plan_topic` the UI posts there.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from utils.db_connect import DBConnect
from utils.keyword_normalizer import normalize_keywords, normalize_single
from schemas.resume_schemas import flatten_skills, build_resume_text

# Role/seniority normalizers + modal helper — the SAME cheap keying every other
# cohort feature uses, so trends line up with the pipeline-intel cohorts.
from services.pipeline_intel_service import _role_family, _modal

# Reuse the differentiation reject vocabulary + "already has it" test verbatim —
# a trend topic must clear the exact same integrity bar as a differentiator.
from services.differentiation_service import (
    _CERT_TOKENS,
    _COURSE_TOKENS,
    _LEARN_PREFIXES,
    _SOFT_TOKENS,
    _BASELINE_SKILLS,
    _user_has,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (all enforced in CODE)
# ---------------------------------------------------------------------------
WINDOWS = (30, 90)               # trailing-day windows we aggregate over
MAX_SCAN_PER_SOURCE = 60000      # bound the nightly cross-user scan per collection
MIN_SKILL_COUNT = 3              # k-anonymity floor: a (role,skill,window) must be
                                 # seen in >= 3 distinct JDs to be stored at all
MIN_ROLE_JDS = 5                 # a role/window needs this many JDs to publish pct
MAX_SKILLS_PER_JD = 40           # ignore absurdly long keyword dumps
ROLE_TOTAL_SENTINEL = "*"        # skill="*" doc carries the role/window JD total
DEFAULT_WINDOW = 90

MAX_TREND_ITEMS = 8              # cap on what trending_for_user returns
GAP_WEIGHT_HAS = 0.15           # de-prioritise (don't hard-drop) skills the user has
OUTCOME_BOOST_PER_HIT = 0.18    # each rejected/ghosted JD that wanted a lacked skill
MAX_OUTCOME_BOOST = 0.6         # ... capped, so outcomes tilt but never dominate

# Outcome statuses that count as "the market pushed back". Mirrors
# gmail_service.VALID_STATUSES / pipeline_intel_service terminal states.
_OUTCOME_STATUSES = ("rejected", "ghosted")

# Learnability heuristic (0..1): how plausibly a motivated candidate can produce a
# real artifact for this topic inside ~2 weeks. This is a RANKING WEIGHT, never a
# promise — broad fields rank below concrete, shippable tools. Deterministic.
_BROAD_TOKENS = (
    "machine learning", "deep learning", "distributed systems", "system design",
    "algorithms", "data structures", "mathematics", "statistics", "compilers",
    "cryptography", "operating systems", "computer vision", "reinforcement learning",
    "natural language processing", "architecture", "leadership", "management",
)
_CONCRETE_TOKENS = (
    "docker", "kubernetes", "terraform", "fastapi", "flask", "django", "react",
    "next.js", "graphql", "grpc", "kafka", "redis", "postgres", "postgresql",
    "mongodb", "airflow", "spark", "dbt", "prometheus", "grafana", "github actions",
    "ci/cd", "pytest", "playwright", "tailwind", "typescript", "rust", "golang",
    "pandas", "numpy", "pytorch", "tensorflow", "langchain", "openai", "bedrock",
    "websockets", "webhooks", "oauth", "stripe", "s3", "lambda", "dynamodb",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _iso(v):
    if not hasattr(v, "isoformat"):
        return v
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


def _as_utc(v) -> Optional[datetime]:
    """Coerce a stored Mongo datetime to an aware UTC datetime (or None)."""
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _is_rejected_topic(skill: str, user_skills: List[str], resume_text: str) -> bool:
    """Apply differentiation_service's reject vocabulary to a candidate topic.

    Drops certifications, course-completions, 'learn X' phrasings, soft skills,
    baseline-everyone-has skills, and anything the user already demonstrates.
    """
    s = (skill or "").strip().lower()
    if not s or len(s) < 2:
        return True
    if any(tok in s for tok in _CERT_TOKENS):
        return True
    if any(tok in s for tok in _COURSE_TOKENS):
        return True
    if any(s.startswith(p) for p in _LEARN_PREFIXES):
        return True
    if any(tok in s for tok in _SOFT_TOKENS):
        return True
    if s in _BASELINE_SKILLS:
        return True
    return False


def _learnability(skill: str) -> float:
    """Deterministic 0..1 weight: concrete shippable tools rank above broad fields."""
    s = (skill or "").strip().lower()
    if not s:
        return 0.0
    if any(tok == s or tok in s for tok in _CONCRETE_TOKENS):
        return 0.9
    if any(tok in s for tok in _BROAD_TOKENS):
        return 0.4
    words = s.split()
    if len(words) == 1 and len(s) <= 24:
        return 0.75          # a single concrete keyword — likely a tool/library
    if len(words) >= 4:
        return 0.45          # a sprawling phrase — usually a broad competency
    return 0.6


# ---------------------------------------------------------------------------
# JD corpus extraction (privacy-safe: we read only skills + title + timestamp)
# ---------------------------------------------------------------------------
def _extract_job_blob(sj: Dict) -> Dict:
    """saved_jobs write the JD under `job_data` (manual save) or `job` (pipeline)."""
    return sj.get("job_data") or sj.get("job") or {}


def _jd_skills(job: Dict) -> List[str]:
    """Normalized, de-duplicated skill set for one JD. Never returns user PII."""
    raw: List[str] = []
    for key in ("required_skills", "preferred_skills", "keywords", "missing_top"):
        raw += [str(s) for s in (job.get(key) or []) if str(s).strip()]
    norm = normalize_keywords(raw)
    out: List[str] = []
    seen = set()
    for s in norm:
        sl = s.lower().strip()
        if sl and sl not in seen and len(sl) >= 2:
            seen.add(sl)
            out.append(s)
        if len(out) >= MAX_SKILLS_PER_JD:
            break
    return out


def _iter_corpus() -> Tuple[List[Tuple[datetime, str, List[str]]], int]:
    """Yield (timestamp, role_family, [skills]) for the whole matched-JD corpus.

    Sources: `saved_jobs.job_data`/`job` (saved_at) and `tailoring_records.jd_analysis`
    (application.applied_at | updated_at | created_at). NO user_email is retained —
    the tuple is anonymous by construction.
    """
    records: List[Tuple[datetime, str, List[str]]] = []
    scanned = 0
    db = _db()

    # 1) saved_jobs — matched/collected JDs across all users.
    try:
        cur = db.saved_jobs.find(
            {}, {"job_data": 1, "job": 1, "saved_at": 1, "_id": 0}
        ).limit(MAX_SCAN_PER_SOURCE)
        for sj in cur:
            scanned += 1
            job = _extract_job_blob(sj)
            title = str(job.get("title") or job.get("job_title") or "").strip()
            if not title:
                continue
            ts = _as_utc(sj.get("saved_at")) or _now()
            skills = _jd_skills(job)
            if skills:
                records.append((ts, _role_family(title), skills))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("trend: saved_jobs scan failed: %s", e)

    # 2) tailoring_records — the JDs users actually applied against (clean JD analysis).
    try:
        cur = db.tailoring_records.find(
            {"jd_analysis": {"$exists": True}},
            {"jd_analysis": 1, "application.applied_at": 1,
             "updated_at": 1, "created_at": 1, "_id": 0},
        ).limit(MAX_SCAN_PER_SOURCE)
        for tr in cur:
            scanned += 1
            jd = tr.get("jd_analysis") or {}
            title = str(jd.get("job_title") or jd.get("title") or "").strip()
            if not title:
                continue
            app = tr.get("application") or {}
            ts = (_as_utc(app.get("applied_at")) or _as_utc(tr.get("updated_at"))
                  or _as_utc(tr.get("created_at")) or _now())
            skills = _jd_skills(jd)
            if skills:
                records.append((ts, _role_family(title), skills))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("trend: tailoring_records scan failed: %s", e)

    return records, scanned


# ---------------------------------------------------------------------------
# recompute_market_stats — the nightly cross-user aggregation
# ---------------------------------------------------------------------------
def recompute_market_stats() -> Dict:
    """Rebuild `skill_market_stats` from the whole matched-JD corpus.

    Frequencies are COUNTED in code (never model-estimated). The write is a clean
    delete-then-insert per window so the collection never accretes stale rows, and
    only (role, skill, window) groups that clear the k-anonymity floor
    (MIN_SKILL_COUNT distinct JDs, MIN_ROLE_JDS per role/window) are published.
    Returns a summary — it stores/exposes ONLY counts, never any user's data.
    """
    corpus, scanned = _iter_corpus()
    now = _now()
    cutoffs = {w: now - timedelta(days=w) for w in WINDOWS}

    # counts[window][role][skill] = distinct-JD count ; totals[window][role] = JDs
    counts: Dict[int, Dict[str, Dict[str, int]]] = {w: {} for w in WINDOWS}
    totals: Dict[int, Dict[str, int]] = {w: {} for w in WINDOWS}

    for ts, role, skills in corpus:
        for w in WINDOWS:
            if ts < cutoffs[w]:
                continue
            totals[w][role] = totals[w].get(role, 0) + 1
            role_counts = counts[w].setdefault(role, {})
            for s in skills:
                role_counts[s] = role_counts.get(s, 0) + 1

    docs: List[Dict] = []
    for w in WINDOWS:
        for role, role_counts in counts[w].items():
            role_total = totals[w].get(role, 0)
            if role_total < MIN_ROLE_JDS:
                continue
            published = 0
            for skill, cnt in role_counts.items():
                if cnt < MIN_SKILL_COUNT:
                    continue
                docs.append({
                    "role": role,
                    "skill": normalize_single(skill),
                    "window": w,
                    "count": cnt,
                    "pct": round(cnt / role_total, 4),
                    "updated_at": now,
                })
                published += 1
            if published:
                # Sentinel row carrying the role/window denominator (JD sample size).
                docs.append({
                    "role": role,
                    "skill": ROLE_TOTAL_SENTINEL,
                    "window": w,
                    "count": role_total,
                    "pct": 1.0,
                    "updated_at": now,
                })

    db = _db()
    try:
        db.skill_market_stats.delete_many({"window": {"$in": list(WINDOWS)}})
        if docs:
            db.skill_market_stats.insert_many(docs, ordered=False)
    except Exception as e:  # pragma: no cover - defensive
        logger.error("trend: skill_market_stats write failed: %s", e)
        raise

    roles = len({(d["role"]) for d in docs})
    summary = {
        "scanned_jds": scanned,
        "corpus_size": len(corpus),
        "rows_written": len(docs),
        "roles": roles,
        "windows": list(WINDOWS),
        "recomputed_at": _iso(now),
    }
    logger.info("trend: recompute_market_stats %s", summary)
    return summary


def build_trend_aggregate_job(payload: Dict) -> Dict:
    """Async entry point for job_type='trend_aggregate' (nightly cross-user recompute).

    Cross-user batch: takes no user_email, spends no per-user entitlement, calls no
    LLM. Just the deterministic aggregate rebuild.
    """
    return recompute_market_stats()


# ---------------------------------------------------------------------------
# trending_for_user — market demand × user gap × learnability (+ outcomes)
# ---------------------------------------------------------------------------
def _user_evidence(user_email: str) -> Dict:
    """Skills the user demonstrably has + resume text (for the 'already has it' test)."""
    db = _db()
    resume_doc = db.user_resumes.find_one(
        {"user_email": user_email, "structured": {"$exists": True}},
        sort=[("parsed_at", -1)],
    ) or {}
    structured = resume_doc.get("structured") or {}
    skills = [s for s in flatten_skills(structured) if s] if structured else []
    resume_text = build_resume_text(structured).lower() if structured else ""

    prefs = db.user_preferences.find_one({"user_email": user_email}) or {}
    pref_skills = [s for s in (prefs.get("current_skills") or []) if s]
    user = db.users.find_one({"email": user_email}, {"earned_skills": 1}) or {}
    earned = [e.get("skill") for e in (user.get("earned_skills") or []) if e.get("skill")]
    all_skills = list(dict.fromkeys([*skills, *pref_skills, *earned]))
    target_roles = [r for r in (prefs.get("target_roles") or []) if r]
    return {
        "skills": all_skills,
        "resume_text": resume_text,
        "target_roles": target_roles,
    }


def _user_role_family(user_email: str, target_roles: List[str]) -> str:
    """The user's normalized role = modal role_family across the JDs they engaged
    with (saved + applied), falling back to their stated target role, then 'other'."""
    titles: List[str] = []
    db = _db()
    try:
        for sj in db.saved_jobs.find(
            {"user_email": user_email}, {"job_data": 1, "job": 1, "_id": 0}
        ).limit(300):
            job = _extract_job_blob(sj)
            t = str(job.get("title") or job.get("job_title") or "").strip()
            if t:
                titles.append(t)
    except Exception as e:  # pragma: no cover
        logger.warning("trend: user role scan (saved_jobs) failed for %s: %s", user_email, e)
    try:
        for tr in db.tailoring_records.find(
            {"user_email": user_email}, {"jd_analysis.job_title": 1, "_id": 0}
        ).limit(300):
            t = str(((tr.get("jd_analysis") or {}).get("job_title")) or "").strip()
            if t:
                titles.append(t)
    except Exception as e:  # pragma: no cover
        logger.warning("trend: user role scan (records) failed for %s: %s", user_email, e)

    if titles:
        fam = _modal([_role_family(t) for t in titles], "other")
        if fam != "other":
            return fam
    for tr in target_roles:
        fam = _role_family(tr)
        if fam != "other":
            return fam
    return "other"


def _outcome_pressure(user_email: str) -> Dict[str, int]:
    """Skills demanded by the JDs this user got REJECTED or GHOSTED on, counted.

    Grounded in Gmail-detected outcomes (tailoring_records.application.status). A
    skill recurring across several failed applications is a louder gap signal.
    Returns {normalized_skill_lower: recurrence_count}.
    """
    pressure: Dict[str, int] = {}
    try:
        cur = _db().tailoring_records.find(
            {
                "user_email": user_email,
                "application.status": {"$in": list(_OUTCOME_STATUSES)},
            },
            {"jd_analysis": 1, "_id": 0},
        ).limit(300)
        for tr in cur:
            for s in _jd_skills(tr.get("jd_analysis") or {}):
                key = s.lower().strip()
                if key:
                    pressure[key] = pressure.get(key, 0) + 1
    except Exception as e:  # pragma: no cover
        logger.warning("trend: outcome pressure failed for %s: %s", user_email, e)
    return pressure


def _market_stats(role: str, window: int) -> Tuple[List[Dict], int]:
    """Return ([{skill, pct, count} ...], role_jd_total) for a role/window."""
    rows = list(_db().skill_market_stats.find(
        {"role": role, "window": int(window)},
        {"skill": 1, "pct": 1, "count": 1, "_id": 0},
    ))
    total = 0
    skills: List[Dict] = []
    for r in rows:
        if r.get("skill") == ROLE_TOTAL_SENTINEL:
            total = int(r.get("count") or 0)
        else:
            skills.append(r)
    return skills, total


def _jobs_unlocked(user_email: str, skill: str, limit: int = 5) -> List[Dict]:
    """Saved jobs whose JD calls for `skill` (reuses learning_service's no-LLM loop)."""
    try:
        from services import learning_service
        return learning_service._jobs_unlocked_by_skill(user_email, skill, limit=limit)
    except Exception as e:  # pragma: no cover
        logger.warning("trend: unlock lookup failed for %s/%s: %s", user_email, skill, e)
        return []


def trending_for_user(
    user_email: str, window: int = DEFAULT_WINDOW, limit: int = MAX_TREND_ITEMS
) -> Dict:
    """Rank learning topics by market demand × user gap × learnability, tilted by
    the user's real application outcomes. Deterministic; no LLM, no spend.

    Each item: {skill, market_pct, market_count, user_has, learnability,
    outcome_pressure, unlocks_jobs, score, plan_topic, unlocked}. `plan_topic` is
    what the UI POSTs to the already-gated /learning/plan to build a plan.
    """
    window = window if window in WINDOWS else DEFAULT_WINDOW
    evidence = _user_evidence(user_email)
    role = _user_role_family(user_email, evidence["target_roles"])

    market, role_total = _market_stats(role, window)
    degraded = False
    if not market and role != "other":
        # No aggregate for their specific role yet — degrade to the 'other' bucket
        # rather than invent demand, and flag it.
        market, role_total = _market_stats("other", window)
        degraded = True
    if not market:
        return {
            "role": role, "window": window, "market_size": role_total,
            "degraded": True, "items": [],
            "note": "Not enough cross-user market signal for this role yet.",
            "generated_at": _iso(_now()),
        }

    pressure = _outcome_pressure(user_email)
    user_skills = evidence["skills"]
    resume_text = evidence["resume_text"]

    items: List[Dict] = []
    for row in market:
        skill = str(row.get("skill") or "").strip()
        if not skill:
            continue
        if _is_rejected_topic(skill, user_skills, resume_text):
            continue
        market_pct = float(row.get("pct") or 0.0)
        user_has = _user_has(skill, user_skills, resume_text)
        learnability = _learnability(skill)
        hits = pressure.get(skill.lower().strip(), 0)
        outcome_boost = min(MAX_OUTCOME_BOOST, OUTCOME_BOOST_PER_HIT * hits)
        gap_weight = GAP_WEIGHT_HAS if user_has else 1.0
        score = round(market_pct * gap_weight * learnability * (1.0 + outcome_boost), 5)
        if score <= 0:
            continue
        items.append({
            "skill": skill,
            "market_pct": round(market_pct, 4),
            "market_count": int(row.get("count") or 0),
            "user_has": user_has,
            "learnability": learnability,
            "outcome_pressure": hits,
            "score": score,
            "plan_topic": skill,
        })

    # Rank: gaps the market wants and outcomes punished float to the top.
    items.sort(key=lambda it: (-it["score"], not it["user_has"]))
    top = items[:limit]

    # Only resolve the (bounded) unlock lookup for what we actually return.
    for it in top:
        unlocked = _jobs_unlocked(user_email, it["skill"], limit=5)
        it["unlocked"] = unlocked
        it["unlocks_jobs"] = len(unlocked)

    return {
        "role": role,
        "window": window,
        "market_size": role_total,
        "degraded": degraded,
        "items": top,
        "generated_at": _iso(_now()),
    }
