"""Learning Hub — Progress Service.

Owns XP, levels, the sprint-local streak, milestone achievements, and the
analytics aggregation. Deliberately separate from the job-application streak
(`parse_streaks`) — this streak counts consecutive days of *completing sprint
challenges*.

Collections: `sprint_progress` (one doc per user+path, the unlock source of
truth) and `sprint_achievements` (one doc per user+achievement, idempotent).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from utils.db_connect import DBConnect
from services import sprint_curriculum as curriculum

logger = logging.getLogger(__name__)


def _db():
    return DBConnect().get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v):
    if not hasattr(v, "isoformat"):
        return v
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


def _today_str(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Achievements — evaluated after each day completion. Idempotent by (email, path, code).
# ---------------------------------------------------------------------------
ACHIEVEMENTS: List[Dict] = [
    {"code": "first_challenge",  "title": "First Challenge",   "description": "Completed your first engineering ticket.",        "tier": "bronze", "xp_bonus": 25},
    {"code": "three_day_streak", "title": "3-Day Streak",      "description": "Shipped three days in a row.",                    "tier": "silver", "xp_bonus": 50},
    {"code": "halfway",          "title": "Halfway There",     "description": "Reached the midpoint of a chapter.",              "tier": "silver", "xp_bonus": 75},
    {"code": "challenge_master", "title": "Challenge Master",  "description": "Completed 12 engineering tickets.",               "tier": "gold",   "xp_bonus": 100},
    {"code": "sprint_complete",  "title": "Chapter Complete",  "description": "Delivered a full chapter. Job well done.",        "tier": "diamond", "xp_bonus": 200},
]
_ACH_BY_CODE = {a["code"]: a for a in ACHIEVEMENTS}

# Milestones that measure progress WITHIN a chapter re-fire per chapter, so their
# stored code is namespaced by season (`halfway:sprint-2`). first_challenge (your
# first-ever ticket) and three_day_streak (overall engagement) stay global.
_PER_SEASON_CODES = {"halfway", "challenge_master", "sprint_complete"}


def _scoped_code(code: str, season_id: Optional[str]) -> str:
    return f"{code}:{season_id}" if (code in _PER_SEASON_CODES and season_id) else code


def _eval_achievement_codes(completed_count: int, streak_current: int, total_days: int) -> List[str]:
    codes = []
    if completed_count >= 1:
        codes.append("first_challenge")
    if streak_current >= 3:
        codes.append("three_day_streak")
    if completed_count >= max(1, total_days // 2):
        codes.append("halfway")
    if completed_count >= min(12, total_days - 2):
        codes.append("challenge_master")
    if completed_count >= total_days:
        codes.append("sprint_complete")
    return codes


def award_achievement(user_email: str, path_key: str, code: str,
                      season_id: Optional[str] = None, label_override: Optional[str] = None) -> Optional[Dict]:
    """Idempotent award. Per-chapter codes are namespaced by season so they can
    re-fire for a new chapter. Returns the achievement dict (with the effective
    label) if newly earned, else None."""
    meta = _ACH_BY_CODE.get(code)
    if not meta:
        return None
    stored_code = _scoped_code(code, season_id)
    label = label_override or meta["title"]
    res = _db().sprint_achievements.update_one(
        {"user_email": user_email, "code": stored_code},
        {"$setOnInsert": {
            "user_email": user_email,
            "path_key": path_key,
            "code": stored_code,
            "base_code": code,
            "season_id": season_id,
            "label": label,
            "tier": meta["tier"],
            "xp_bonus": meta["xp_bonus"],
            "earned_at": _now(),
        }},
        upsert=True,
    )
    return {**meta, "title": label} if res.upserted_id is not None else None


def list_achievements(user_email: str) -> List[Dict]:
    """All milestones with unlocked/locked state for the achievements grid. A
    per-chapter milestone counts as unlocked if earned in ANY chapter (matched by
    base_code, falling back to the raw code for legacy global rows)."""
    earned_by_base: Dict[str, Dict] = {}
    for a in _db().sprint_achievements.find({"user_email": user_email}):
        base = a.get("base_code") or a.get("code")
        # Keep the earliest earn per base milestone.
        if base not in earned_by_base:
            earned_by_base[base] = a
    out = []
    for meta in ACHIEVEMENTS:
        e = earned_by_base.get(meta["code"])
        out.append({
            "id": meta["code"],
            "title": meta["title"],
            "description": meta["description"],
            "tier": meta["tier"],
            "xp_bonus": meta["xp_bonus"],
            "unlocked": e is not None,
            "unlocked_at": _iso(e["earned_at"]) if e else None,
        })
    return out


# ---------------------------------------------------------------------------
# Day completion — XP, streak, level, achievements. Called by submission service.
# ---------------------------------------------------------------------------
def on_day_completed(progress_doc: Dict, day_key: str, submission_id: str) -> Dict:
    """Apply rewards for completing `day_key`. Returns a reward summary.

    Assumes the caller has already validated the day was `current` and set
    `day_status[day_key].completed_at` atomically (so this can't double-award).
    """
    user_email = progress_doc["user_email"]
    path_key = progress_doc["path_key"]
    season_id = progress_doc.get("season_id", curriculum.DEFAULT_SEASON)
    total_days = len(curriculum.spine_for_season(season_id))

    day_xp = curriculum.xp_for_day(season_id, day_key)

    # Streak: reload the freshest doc to count completed days + streak math.
    fresh = _db().sprint_progress.find_one({"user_email": user_email, "path_key": path_key}) or progress_doc
    day_status = fresh.get("day_status", {})
    completed_count = sum(1 for v in day_status.values() if v.get("completed_at"))

    today = _today_str()
    last = fresh.get("last_completed_date")
    streak_current = int(fresh.get("streak_current") or 0)
    if last == today:
        pass  # already counted a completion today
    elif last == _today_str(_now() - timedelta(days=1)):
        streak_current += 1
    else:
        streak_current = 1
    streak_longest = max(int(fresh.get("streak_longest") or 0), streak_current)

    new_xp = int(fresh.get("xp") or 0) + day_xp

    # Achievements (idempotent) — collect newly-earned + their bonus XP. The
    # chapter-completion badge gets a dynamic, story-aware title.
    season = curriculum.season_meta(season_id)
    chapter_title = season.get("title") or season.get("label") or "Sprint"
    newly: List[Dict] = []
    for code in _eval_achievement_codes(completed_count, streak_current, total_days):
        label_override = f"{chapter_title} Complete" if code == "sprint_complete" else None
        got = award_achievement(user_email, path_key, code, season_id=season_id, label_override=label_override)
        if got:
            newly.append(got)
            new_xp += got["xp_bonus"]

    new_level = curriculum.level_for_xp(new_xp)
    prev_level = int(fresh.get("level") or 1)

    _db().sprint_progress.update_one(
        {"user_email": user_email, "path_key": path_key},
        {"$set": {
            "xp": new_xp,
            "level": new_level,
            "streak_current": streak_current,
            "streak_longest": streak_longest,
            "last_completed_date": today,
            "completed_count": completed_count,
            "updated_at": _now(),
        }},
    )

    return {
        "xp": new_xp,
        "xp_gained": day_xp + sum(a["xp_bonus"] for a in newly),
        "level": new_level,
        "leveled_up": new_level > prev_level,
        "streak_current": streak_current,
        "streak_longest": streak_longest,
        "completed_count": completed_count,
        "achievement_unlocked": (
            {
                "id": newly[0]["code"],
                "title": newly[0]["title"],
                "description": newly[0]["description"],
                "tier": newly[0]["tier"],
                "xp_bonus": newly[0]["xp_bonus"],
                "unlocked": True,
                "unlocked_at": _iso(_now()),
            }
            if newly else None
        ),
    }


# ---------------------------------------------------------------------------
# Analytics aggregation
# ---------------------------------------------------------------------------
def _parse_hours(est_time: Optional[str], difficulty: str) -> float:
    """Best-effort parse of a free-text estimate ("3-4 hours") to hours."""
    if est_time:
        nums = re.findall(r"\d+(?:\.\d+)?", str(est_time))
        if nums:
            vals = [float(n) for n in nums]
            hours = sum(vals) / len(vals)
            if "min" in str(est_time).lower() and "hour" not in str(est_time).lower():
                hours = hours / 60.0
            return round(hours, 1)
    return {"junior": 2.0, "mid": 3.0, "senior": 4.0}.get(difficulty, 3.0)


def compute_analytics(user_email: str, path_key: str, path_doc: Dict, progress_doc: Dict) -> Dict:
    season_id = progress_doc.get("season_id", curriculum.DEFAULT_SEASON)
    spine = curriculum.spine_for_season(season_id)
    total_days = len(spine)
    day_status = progress_doc.get("day_status", {})
    difficulty = progress_doc.get("difficulty", "mid")

    days_by_key = {d["key"]: d for d in (path_doc.get("days") or [])}
    theme_by_key = {d["key"]: d["theme"] for d in spine}

    completed = [
        (k, v) for k, v in day_status.items() if v.get("completed_at")
    ]
    completed.sort(key=lambda kv: kv[1]["completed_at"])
    completed_count = len(completed)

    challenge_history = []
    learning_hours = 0.0
    projects_built = 0
    for key, entry in completed:
        day_doc = days_by_key.get(key) or {}
        ticket = day_doc.get("ticket") or {}
        learning_hours += _parse_hours(ticket.get("estimated_time"), difficulty)
        if theme_by_key.get(key) in ("build", "capstone"):
            projects_built += 1
        challenge_history.append({
            "day": day_doc.get("day"),
            "key": key,
            "title": ticket.get("title") or key,
            "completed_at": _iso(entry.get("completed_at")),
            "xp": curriculum.xp_for_day(season_id, key),
        })

    current_day = completed_count + 1 if completed_count < total_days else total_days

    # Recent activity: recent submissions + achievements, merged + sorted desc.
    activity: List[Dict] = []
    for s in _db().sprint_submissions.find({"user_email": user_email, "path_key": path_key}).sort("submitted_at", -1).limit(10):
        activity.append({
            "ts": _iso(s.get("submitted_at")),
            "type": "submission",
            "label": f"Submitted {((days_by_key.get(s.get('day_key')) or {}).get('ticket') or {}).get('title') or s.get('day_key')}",
        })
    for a in _db().sprint_achievements.find({"user_email": user_email}).sort("earned_at", -1).limit(10):
        activity.append({"ts": _iso(a.get("earned_at")), "type": "achievement", "label": f"Unlocked “{a.get('label')}”"})
    activity.sort(key=lambda x: x.get("ts") or "", reverse=True)

    # Incremental rollout: only some days are released (per-season gate). Cap
    # "current day" at the last released day; don't project a completion date we
    # can't schedule.
    available_days = sum(1 for sd in spine if curriculum.is_published(sd["day"], season_id))
    current_day = min(current_day, available_days) if available_days else current_day
    fully_published = available_days >= total_days
    remaining = max(0, total_days - completed_count)
    est_completion = (_now() + timedelta(hours=24 * remaining)).strftime("%Y-%m-%d") if fully_published else None

    return {
        "career_path": path_doc.get("role_label", ""),
        "difficulty": difficulty,
        "current_day": current_day,
        "total_days": total_days,
        "available_days": available_days,
        "overall_progress": completed_count,
        "completion_pct": round(100 * completed_count / total_days) if total_days else 0,
        "completed_count": completed_count,
        "tasks_completed": completed_count,
        "projects_built": projects_built,
        "xp": int(progress_doc.get("xp") or 0),
        "level": int(progress_doc.get("level") or 1),
        "current_streak": int(progress_doc.get("streak_current") or 0),
        "longest_streak": int(progress_doc.get("streak_longest") or 0),
        "learning_hours": round(learning_hours, 1),
        "estimated_completion_date": est_completion,
        "challenge_history": challenge_history,
        "recent_activity": activity[:12],
    }
