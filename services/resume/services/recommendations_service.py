"""Personalized "Recommended for you" engine (Pillar A).

The all-in-one payoff: a single ranked feed of next-best-actions synthesized
from EVERY surface — saved jobs, applications, learning paths, interview
attempts, events, and preferences. Candidate generation is deterministic and
cheap (no LLM needed); Premium can layer a FLASH "senior recruiter" nudge on
top. Each item carries a `tab` so the UI deep-links into the right place.
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


def _safe(fn) -> List[Dict]:
    try:
        return fn() or []
    except Exception as e:
        logger.warning("recommendation generator failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Candidate generators — each returns a list of recommendation dicts.
# ---------------------------------------------------------------------------
def _stale_applications(email) -> List[Dict]:
    cutoff = _now() - timedelta(days=7)
    out = []
    for r in _db().tailoring_records.find(
        {"user_email": email, "application.status": {"$in": ["applied", "interviewing"]}, "deleted": {"$ne": True}}
    ).limit(50):
        app = r.get("application") or {}
        updated = _aware(r.get("updated_at"))
        if updated and updated < cutoff:
            company = (r.get("jd_analysis") or {}).get("company") or "this role"
            out.append({
                "id": f"stale:{r.get('record_id')}", "type": "followup", "icon": "clock",
                "title": f"Follow up on {company}", "subtitle": f"No movement in a week — a nudge keeps you top of mind.",
                "cta": "Open applications", "tab": "applications", "priority": 90,
            })
    return out[:3]


def _untailored_saved_jobs(email) -> List[Dict]:
    out = []
    for sj in _db().saved_jobs.find({"user_email": email, "status": {"$in": ["interested", "saved", None]}}).limit(20):
        job = sj.get("job") or {}
        title = job.get("title") or "a saved role"
        company = job.get("company")
        out.append({
            "id": f"tailor:{job.get('id') or sj.get('_id')}", "type": "tailor", "icon": "file",
            "title": f"Tailor your resume for {title}", "subtitle": f"{company or 'Saved job'} · ATS-scored in seconds.",
            "cta": "Tailor now", "tab": "tailor", "priority": 65,
        })
    return out[:2]


def _learning_in_progress(email) -> List[Dict]:
    out = []
    progress = {p["path_id"]: p for p in _db().learning_progress.find({"user_email": email})}
    for p in _db().learning_paths.find({"user_email": email, "status": "active"}).limit(10):
        prog = progress.get(p["path_id"], {})
        done = len(set(prog.get("completed_step_ids", [])))
        total = len(p.get("modules", []))
        if total and done < total:
            out.append({
                "id": f"learn:{p['path_id']}", "type": "learning", "icon": "grad",
                "title": f"Continue “{p.get('title')}”",
                "subtitle": f"{done}/{total} modules · each one can unlock matching jobs.",
                "cta": "Resume path", "tab": "learning", "priority": 70,
            })
    return out[:2]


def _interview_weak_spot(email) -> List[Dict]:
    attempts = list(_db().interview_attempts.find({"user_email": email}).sort("answered_at", -1).limit(20))
    if len(attempts) < 2:
        if not attempts:
            return [{
                "id": "mock:first", "type": "interview", "icon": "mic",
                "title": "Run your first mock interview", "subtitle": "We’ll start spotting your answer patterns across roles.",
                "cta": "Practice", "tab": "interview", "priority": 55,
            }]
        return []
    dims: Dict[str, List[int]] = {}
    for a in attempts:
        for d, v in (a.get("dimensions") or {}).items():
            if isinstance(v, (int, float)):
                dims.setdefault(d, []).append(int(v))
    if not dims:
        return []
    avg = {d: sum(v) / len(v) for d, v in dims.items()}
    weakest = min(avg, key=avg.get)
    if avg[weakest] < 70:
        label = weakest.replace("_", " ")
        return [{
            "id": f"weak:{weakest}", "type": "interview", "icon": "mic",
            "title": f"Your interviews are weakest on {label}",
            "subtitle": "A focused mock drill is the fastest fix.",
            "cta": "Drill it", "tab": "interview", "priority": 82,
        }]
    return []


def _skill_gap(email) -> List[Dict]:
    prefs = _db().user_preferences.find_one({"user_email": email}) or {}
    have = {s.lower() for s in (prefs.get("current_skills") or [])}
    user = _db().users.find_one({"email": email}, {"earned_skills": 1}) or {}
    have |= {e.get("skill", "").lower() for e in (user.get("earned_skills") or [])}
    counts: Dict[str, int] = {}
    for sj in _db().saved_jobs.find({"user_email": email}).limit(50):
        job = sj.get("job") or {}
        for s in (job.get("missing_top") or []) + (job.get("required_skills") or []):
            sl = str(s).lower()
            if sl and sl not in have:
                counts[str(s)] = counts.get(str(s), 0) + 1
    if not counts:
        return []
    skill, n = max(counts.items(), key=lambda kv: kv[1])
    if n < 2:
        return []
    return [{
        "id": f"gap:{skill}", "type": "learning", "icon": "grad",
        "title": f"Learn {skill} to unlock {n} saved jobs",
        "subtitle": "Generate a focused study plan for it.",
        "cta": "Build a plan", "tab": "learning", "priority": 62,
    }]


def _matching_events(email) -> List[Dict]:
    prefs = _db().user_preferences.find_one({"user_email": email}) or {}
    interest = {s.lower() for s in (prefs.get("current_skills") or []) + (prefs.get("learning_interests") or []) + (prefs.get("target_roles") or [])}
    if not interest:
        return []
    out = []
    for e in _db().events.find({"status": "active", "legitimacy_band": {"$in": ["verified", "credible"]}}).sort("legitimacy_score", -1).limit(40):
        tags = {str(t).lower() for t in (e.get("tags") or [])}
        text = (e.get("title", "") + " " + (e.get("description") or "")).lower()
        if tags & interest or any(i in text for i in interest if len(i) > 3):
            out.append({
                "id": f"event:{e.get('event_id')}", "type": "event", "icon": "trophy",
                "title": f"{e.get('title')} matches your focus",
                "subtitle": f"{e.get('legitimacy_band', '').title()} · {e.get('org') or 'event'}.",
                "cta": "See event", "tab": "events", "priority": 48,
            })
        if len(out) >= 2:
            break
    return out


GENERATORS = [
    _stale_applications, _interview_weak_spot, _learning_in_progress,
    _untailored_saved_jobs, _skill_gap, _matching_events,
]


def get_recommendations(email: str, limit: int = 6) -> List[Dict]:
    items: List[Dict] = []
    for gen in GENERATORS:
        items.extend(_safe(lambda g=gen: g(email)))
    # de-dupe by id, keep highest priority, sort desc
    seen: Dict[str, Dict] = {}
    for it in items:
        cur = seen.get(it["id"])
        if not cur or it["priority"] > cur["priority"]:
            seen[it["id"]] = it
    ranked = sorted(seen.values(), key=lambda x: -x.get("priority", 0))
    return ranked[:limit]
