"""Cross-user ATS keyword signals (Craft-overhaul Part B3).

For a role family, which JD keywords correlate with HIGHER ATS scores? Computed
across many users' tailoring_records and stored as pure aggregate statistics.

FIREWALL (the critical one) — aggregates ONLY. No verbatim bullet, claim,
company, metric, resume text, or user identity ever crosses between users or
lands in the output collection. The corpus read projects only `jd_analysis`
(keywords + title) and `ats_scores.overall` (a number); the stored docs hold
only `{role, keyword, jd_count, mean_ats, lift}`. k-anonymity floors ensure a
keyword is published only when it appears across enough distinct JDs that it
cannot identify one user.

The consumer side receives a STRATEGY HINT ("for backend roles, `kubernetes`
correlates with higher ATS — emphasize it if truthful"), never content to
insert. Fabrication guards still gate the output.

Mirrors trend_signal_service (the existing cross-user aggregate) in structure:
delete-then-insert recompute, run as a scheduled `system@internal` job.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

STATS_COLLECTION = "jd_keyword_ats_stats"
ROLE_TOTAL_SENTINEL = "*"  # per-role denominator row (keyword == "*")

# Privacy / significance floors — a (role, keyword) is published only if it
# appears in at least this many DISTINCT scored JDs (k-anonymity), and a role is
# published only if it has at least this many scored JDs overall.
MIN_KEYWORD_JDS = 3
MIN_ROLE_JDS = 5

# Only keywords whose mean ATS beats the role baseline by this margin are worth
# surfacing as a positive signal.
MIN_LIFT = 2.0

MAX_SCAN = 20000
MAX_HINT_KEYWORDS = 8


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _jd_keywords(jd: Dict[str, Any]) -> List[str]:
    """Normalized, deduped JD keyword set (required + preferred + keywords)."""
    from utils.keyword_normalizer import normalize_keywords

    raw: List[str] = []
    for key in ("required_skills", "preferred_skills", "keywords"):
        raw.extend([k for k in (jd.get(key) or []) if isinstance(k, str) and k.strip()])
    return normalize_keywords(raw)


def _iter_corpus() -> List[Tuple[str, List[str], int]]:
    """Cross-user corpus as anonymous (role_family, [keywords], overall_ats).

    Reads ONLY jd_analysis + ats_scores.overall — never tailored_resume,
    company, or user_email. Skips soft-deleted and unscored records.
    """
    from services.pipeline_intel_service import _role_family

    cur = _db().tailoring_records.find(
        {"jd_analysis": {"$exists": True}, "deleted": {"$ne": True}},
        {"jd_analysis": 1, "ats_scores": 1, "_id": 0},  # NO user_email, NO resume text
    ).limit(MAX_SCAN)

    out: List[Tuple[str, List[str], int]] = []
    for tr in cur:
        jd = tr.get("jd_analysis") or {}
        ats = tr.get("ats_scores") or {}
        overall = ats.get("overall")
        if not isinstance(overall, (int, float)):
            continue  # unscored record — cannot contribute to a score correlation
        title = str(jd.get("job_title") or "").strip()
        role = _role_family(title)
        keywords = _jd_keywords(jd)
        if keywords:
            out.append((role, keywords, int(overall)))
    return out


def recompute_ats_signals() -> Dict[str, Any]:
    """Recompute (role, keyword) → mean-ATS lift across all users. Deterministic.

    Writes only aggregate stats to STATS_COLLECTION (delete-then-insert). Returns
    a small summary (counts) — never any per-user or verbatim data.
    """
    corpus = _iter_corpus()

    # role -> list of scores; (role, keyword) -> list of scores.
    role_scores: Dict[str, List[int]] = {}
    pair_scores: Dict[Tuple[str, str], List[int]] = {}
    for role, keywords, overall in corpus:
        role_scores.setdefault(role, []).append(overall)
        for kw in set(keywords):  # count each keyword once per JD
            pair_scores.setdefault((role, kw), []).append(overall)

    now = _now()
    docs: List[Dict[str, Any]] = []
    for role, scores in role_scores.items():
        if len(scores) < MIN_ROLE_JDS:
            continue
        role_mean = sum(scores) / len(scores)
        # Denominator sentinel row (no keyword text — just the role baseline).
        docs.append({
            "role": role,
            "keyword": ROLE_TOTAL_SENTINEL,
            "jd_count": len(scores),
            "mean_ats": round(role_mean, 2),
            "lift": 0.0,
            "updated_at": now,
        })
        for (r, kw), kw_scores in pair_scores.items():
            if r != role or len(kw_scores) < MIN_KEYWORD_JDS:
                continue
            kw_mean = sum(kw_scores) / len(kw_scores)
            lift = round(kw_mean - role_mean, 2)
            docs.append({
                "role": role,
                "keyword": kw,          # already normalized; a generic term, not user text
                "jd_count": len(kw_scores),
                "mean_ats": round(kw_mean, 2),
                "lift": lift,
                "updated_at": now,
            })

    coll = _db()[STATS_COLLECTION]
    coll.delete_many({})
    if docs:
        coll.insert_many(docs, ordered=False)

    return {
        "roles": len([d for d in docs if d["keyword"] == ROLE_TOTAL_SENTINEL]),
        "keyword_rows": len([d for d in docs if d["keyword"] != ROLE_TOTAL_SENTINEL]),
        "scanned": len(corpus),
    }


def high_signal_keywords(job_title: str, limit: int = MAX_HINT_KEYWORDS) -> List[str]:
    """Keywords that correlate with higher ATS for this job's role family.

    Returns normalized keyword STRINGS only — a strategy hint, never content.
    Fails open to [] (no signal → no hint). Never raises.
    """
    try:
        from services.pipeline_intel_service import _role_family

        role = _role_family(str(job_title or "").strip())
        rows = list(
            _db()[STATS_COLLECTION].find(
                {"role": role, "keyword": {"$ne": ROLE_TOTAL_SENTINEL},
                 "lift": {"$gte": MIN_LIFT}},
                {"keyword": 1, "lift": 1, "_id": 0},
            ).sort("lift", -1).limit(limit)
        )
        return [r["keyword"] for r in rows if r.get("keyword")]
    except Exception as e:
        logger.warning("high_signal_keywords: read failed: %s", e)
        return []


def build_ats_signal_aggregate_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Async job entry point — cross-user batch, no user_email, no LLM.

    Mirrors trend_signal_service.build_trend_aggregate_job.
    """
    return recompute_ats_signals()


def _now():
    # Injected via a helper so tests can stub; production uses UTC now.
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
