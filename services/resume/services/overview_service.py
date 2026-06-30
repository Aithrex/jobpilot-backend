"""Cross-feature dashboard overview (the interconnection layer).

One cheap aggregation that maps a user's state across EVERY surface —
applications, resume, jobs, learning, interviews, events, skills/gaps, momentum
— so the dashboard (and any page) can render an interconnected snapshot and
deep-link/cross-link between features. Deterministic, no LLM.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt if (dt is None or getattr(dt, "tzinfo", None)) else dt.replace(tzinfo=timezone.utc)


def get_overview(email: str) -> Dict:
    db = _db()
    now = _now()
    week_ago = now - timedelta(days=7)

    # ── Applications (from tailoring_records) ──
    apps = list(db.tailoring_records.find(
        {"user_email": email, "deleted": {"$ne": True}},
        {"application": 1, "updated_at": 1, "interview_prep": 1},
    ))
    by_status: Dict[str, int] = {}
    stale = 0
    interview_prep_count = 0
    for r in apps:
        st = (r.get("application") or {}).get("status") or "draft"
        by_status[st] = by_status.get(st, 0) + 1
        if r.get("interview_prep"):
            interview_prep_count += 1
        if st in ("applied", "interviewing"):
            upd = _aware(r.get("updated_at"))
            if upd and upd < week_ago:
                stale += 1
    total_apps = len(apps)

    # ── Resume / jobs ──
    has_base = db.user_resumes.count_documents({"user_email": email, "type": "base"}) > 0
    saved_jobs = db.saved_jobs.count_documents({"user_email": email})

    # ── Learning ──
    progress = list(db.learning_progress.find({"user_email": email}))
    prog_by = {p["path_id"]: p for p in progress}
    modules_completed = sum(len(set(p.get("completed_step_ids", []))) for p in progress)
    active_paths_docs = list(db.learning_paths.find({"user_email": email, "status": "active"}).sort("updated_at", -1).limit(3))
    in_progress: List[Dict] = []
    for p in active_paths_docs:
        total = len(p.get("modules", []))
        done = len(set(prog_by.get(p["path_id"], {}).get("completed_step_ids", [])))
        in_progress.append({
            "path_id": p["path_id"], "title": p.get("title"),
            "percent": round(100 * done / total) if total else 0,
        })
    active_paths = db.learning_paths.count_documents({"user_email": email, "status": "active"})
    user = db.users.find_one({"email": email}, {"earned_skills": 1}) or {}
    earned_skills = [e.get("skill") for e in (user.get("earned_skills") or []) if e.get("skill")]

    # ── Interview ──
    attempts = list(db.interview_attempts.find({"user_email": email}).sort("answered_at", -1).limit(20))
    iv: Dict = {"count": 0}
    try:
        from services.interview_coach_service import _aggregate
        iv = _aggregate(attempts)
    except Exception as e:
        logger.warning("overview interview agg failed: %s", e)

    # ── Events ──
    ue = list(db.user_events.find({"user_email": email}))
    ev_rel: Dict[str, int] = {}
    for x in ue:
        ev_rel[x.get("relation")] = ev_rel.get(x.get("relation"), 0) + 1
    upcoming_verified = db.events.count_documents(
        {"status": "active", "legitimacy_band": {"$in": ["verified", "credible"]}}
    )

    # ── Skills + gaps (the cross-link fuel) ──
    prefs = db.user_preferences.find_one({"user_email": email}) or {}
    have = {s.lower() for s in (prefs.get("current_skills") or [])} | {s.lower() for s in earned_skills}
    gap_counts: Dict[str, int] = {}
    for sj in db.saved_jobs.find({"user_email": email}).limit(60):
        job = sj.get("job") or {}
        for s in (job.get("missing_top") or []) + (job.get("required_skills") or []):
            sl = str(s).lower()
            if sl and sl not in have:
                gap_counts[str(s)] = gap_counts.get(str(s), 0) + 1
    top_gaps = [{"skill": k, "count": v} for k, v in sorted(gap_counts.items(), key=lambda kv: -kv[1])[:5]]

    # ── Momentum ──
    usage = {"used": 0, "limit": 5, "remaining": 5}
    try:
        from services.usage_service import get_usage
        usage = get_usage(email)
    except Exception:
        pass

    return {
        "applications": {
            "total": total_apps,
            "by_status": by_status,
            "active": by_status.get("applied", 0) + by_status.get("interviewing", 0),
            "interviewing": by_status.get("interviewing", 0),
            "offers": by_status.get("offer", 0),
            "stale": stale,
            "interview_prep_count": interview_prep_count,
        },
        "resume": {"has_base": has_base, "tailored_count": total_apps},
        "jobs": {"saved": saved_jobs},
        "learning": {
            "active_paths": active_paths,
            "modules_completed": modules_completed,
            "earned_skills": earned_skills[:12],
            "earned_skills_count": len(earned_skills),
            "in_progress": in_progress,
        },
        "interview": {
            "attempts": iv.get("count", 0),
            "avg_score": iv.get("avg_score"),
            "weakest": (iv.get("weakest_dimensions") or [None])[0],
            "top_weak_spots": iv.get("top_weak_spots", []),
        },
        "events": {
            "saved": ev_rel.get("saved", 0),
            "registered": ev_rel.get("registered", 0),
            "attended": ev_rel.get("attended", 0),
            "won": ev_rel.get("won", 0),
            "upcoming_verified": upcoming_verified,
        },
        "skills": {"have_count": len(have), "top_gaps": top_gaps},
        "momentum": {
            "tailors_today": usage.get("used", 0),
            "tailor_limit": usage.get("limit"),
            "tailors_remaining": usage.get("remaining"),
        },
    }
