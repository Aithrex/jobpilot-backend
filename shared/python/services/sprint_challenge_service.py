"""Learning Hub — Challenge Service.

Resolves a user's canonical (role, difficulty, curriculum_version, season) into
a shared cached path, owns the per-day STATE MACHINE (server-authoritative), and
builds the view the frontend renders.

Cache model: `sprint_paths` docs are keyed by `path_key` and SHARED across all
users with the same (role, difficulty, version, season). Per-user unlock state
lives in `sprint_progress`. Admin previews use an isolated `preview:` path_key +
`is_preview` progress so they never touch a real user's data.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from utils.db_connect import DBConnect
from services import career_paths
from services import sprint_curriculum as curriculum
from services import sprint_progress_service as progress_service

logger = logging.getLogger(__name__)

# 24h in prod; overridable for local dev/testing (e.g. SPRINT_UNLOCK_HOURS=0.001).
UNLOCK_DELAY = timedelta(hours=float(os.getenv("SPRINT_UNLOCK_HOURS", "24")))
UNLOCKING_SOON_WINDOW = timedelta(hours=1)


def _db():
    return DBConnect().get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v):
    if not hasattr(v, "isoformat"):
        return v
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


def _aware(dt):
    """Mongo may return naive datetimes; treat them as UTC for safe math."""
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Path key resolution
# ---------------------------------------------------------------------------
def build_path_key(role_key: str, difficulty: str, season_id: str, preview: bool = False) -> str:
    version = curriculum.SEASONS.get(season_id, curriculum.SEASONS[curriculum.DEFAULT_SEASON])["curriculum_version"]
    base = f"{role_key}:{difficulty}:v{version}:{season_id}"
    return f"preview:{base}" if preview else base


def resolve_context(user_email: str,
                    override_role: Optional[str] = None,
                    override_difficulty: Optional[str] = None,
                    is_admin: bool = False,
                    season_id: str = curriculum.DEFAULT_SEASON) -> Dict:
    """Resolve the effective (role, difficulty, path_key) for this request.

    Admin overrides are honored ONLY when is_admin — they map free text through
    the registry (whitelisted to canonical keys, never raw into a query) and
    produce an isolated preview path_key.
    """
    preview = False
    role_key, difficulty = None, None

    if is_admin and (override_role or override_difficulty):
        preview = True
        if override_role:
            role_key = override_role if career_paths.is_valid_role(override_role) else career_paths.map_role(override_role)
        if override_difficulty and override_difficulty in career_paths.DIFFICULTY_TIERS:
            difficulty = override_difficulty

    if role_key is None or difficulty is None:
        user = _db().users.find_one({"email": user_email}) or {}
        prefs = _db().user_preferences.find_one({"user_email": user_email}) or {}
        r_role, r_diff = career_paths.resolve_for_user(user, prefs)
        role_key = role_key or r_role
        difficulty = difficulty or r_diff

    season = curriculum.SEASONS.get(season_id, curriculum.SEASONS[curriculum.DEFAULT_SEASON])
    meta = career_paths.path_meta(role_key)
    return {
        "role_key": role_key,
        "role_label": meta["label"],
        "role_code": meta["code"],
        "difficulty": difficulty,
        "season_id": season["season_id"],
        "curriculum_version": season["curriculum_version"],
        "path_key": build_path_key(role_key, difficulty, season["season_id"], preview),
        "is_preview": preview,
    }


# ---------------------------------------------------------------------------
# Progress doc lifecycle
# ---------------------------------------------------------------------------
def get_or_init_progress(user_email: str, ctx: Dict) -> Dict:
    path_key = ctx["path_key"]
    doc = _db().sprint_progress.find_one({"user_email": user_email, "path_key": path_key})
    if doc:
        return doc
    now = _now()
    doc = {
        "user_email": user_email,
        "path_key": path_key,
        "season_id": ctx["season_id"],
        "curriculum_version": ctx["curriculum_version"],
        "canonical_role": ctx["role_key"],
        "difficulty": ctx["difficulty"],
        "is_preview": ctx["is_preview"],
        "day_status": {},
        "current_day": 1,
        "xp": 0,
        "level": 1,
        "streak_current": 0,
        "streak_longest": 0,
        "last_completed_date": None,
        "completed_count": 0,
        "started_at": now,
        "created_at": now,
        "updated_at": now,
    }
    _db().sprint_progress.update_one(
        {"user_email": user_email, "path_key": path_key},
        {"$setOnInsert": doc},
        upsert=True,
    )
    return _db().sprint_progress.find_one({"user_email": user_email, "path_key": path_key}) or doc


# ---------------------------------------------------------------------------
# The day-state machine (server-authoritative)
# ---------------------------------------------------------------------------
def compute_day_states(season_id: str, progress_doc: Dict, now: Optional[datetime] = None) -> List[Dict]:
    """Derive each day's state from timestamps only. Never stores 'unlocked'.

    States: completed | current | unlocking_soon | available_tomorrow | locked
            | coming_soon.

    Days beyond `PUBLISHED_THROUGH_DAY` have no content yet, so regardless of the
    unlock math they render as `coming_soon` (no countdown). Publishing more days
    later — by bumping the curriculum gate — automatically re-enables the normal
    unlock states for them with zero changes here.
    """
    now = now or _now()
    spine = curriculum.spine_for_season(season_id)
    day_status = progress_doc.get("day_status", {})
    out: List[Dict] = []

    for i, sd in enumerate(spine):
        key = sd["key"]
        entry = day_status.get(key) or {}
        completed_at = _aware(entry.get("completed_at"))
        prev_completed = None
        if i > 0:
            prev_completed = _aware((day_status.get(spine[i - 1]["key"]) or {}).get("completed_at"))

        unlock_at = None
        seconds_remaining = None

        if not curriculum.is_published(sd["day"]):
            # Not released in this version — a stable "coming soon" node. Even if
            # the previous day is complete, we do NOT start a countdown.
            state = "coming_soon"
        elif completed_at:
            state = "completed"
        elif i == 0:
            state = "current"
        elif not prev_completed:
            state = "locked"
        else:
            unlock_at = prev_completed + UNLOCK_DELAY
            if now >= unlock_at:
                state = "current"
            else:
                remaining = (unlock_at - now).total_seconds()
                seconds_remaining = int(remaining)
                state = "unlocking_soon" if remaining <= UNLOCKING_SOON_WINDOW.total_seconds() else "available_tomorrow"

        out.append({
            "day": sd["day"],
            "key": key,
            "objective": sd["objective"],
            "theme": sd["theme"],
            "state": state,
            "published": curriculum.is_published(sd["day"]),
            "completed_at": _iso(completed_at),
            "unlock_at": _iso(unlock_at),
            "seconds_remaining": seconds_remaining,
        })
    return out


def current_day_number(day_states: List[Dict]) -> int:
    """First non-completed day, capped at the last published day so we never
    point at an unreleased day."""
    n = day_states[-1]["day"] if day_states else 1
    for d in day_states:
        if d["state"] != "completed":
            n = d["day"]
            break
    return min(n, curriculum.PUBLISHED_THROUGH_DAY) if curriculum.PUBLISHED_THROUGH_DAY else n


# ---------------------------------------------------------------------------
# View building
# ---------------------------------------------------------------------------
def get_path_doc(path_key: str) -> Optional[Dict]:
    return _db().sprint_paths.find_one({"path_key": path_key})


def build_path_view(user_email: str, ctx: Dict) -> Optional[Dict]:
    """Full stateful view for the frontend. Returns None if the path isn't ready.

    This is what the generation job returns (so the poll resolves directly to a
    renderable view) and what GET /hub/path returns when ready.
    """
    path_doc = get_path_doc(ctx["path_key"])
    if not path_doc or path_doc.get("status") != "ready":
        return None

    progress = get_or_init_progress(user_email, ctx)
    day_states = compute_day_states(ctx["season_id"], progress)
    states_by_key = {d["key"]: d for d in day_states}
    tickets_by_key = {d["key"]: d.get("ticket") for d in (path_doc.get("days") or [])}

    days = []
    for st in day_states:
        days.append({**st, "ticket": tickets_by_key.get(st["key"])})

    xp = int(progress.get("xp") or 0)
    return {
        "status": "ready",
        "path_key": ctx["path_key"],
        "career_path": ctx["role_label"],
        "role": ctx["role_key"],
        "difficulty": ctx["difficulty"],
        "difficulty_label": career_paths.DIFFICULTY_LABELS.get(ctx["difficulty"], ctx["difficulty"]),
        "season_id": ctx["season_id"],
        "season_length": len(day_states),
        "curriculum_version": ctx["curriculum_version"],
        "is_preview": ctx["is_preview"],
        "published_through_day": curriculum.PUBLISHED_THROUGH_DAY,
        "current_day": current_day_number(day_states),
        "days": days,
        "xp": xp,
        "level": curriculum.level_for_xp(xp),
        "next_level_xp": curriculum.next_level_xp(xp),
        "current_streak": int(progress.get("streak_current") or 0),
        "longest_streak": int(progress.get("streak_longest") or 0),
    }


def get_day(user_email: str, ctx: Dict, day_key: str) -> Optional[Dict]:
    """Single day + ticket. Ticket withheld when the day is locked/waiting."""
    path_doc = get_path_doc(ctx["path_key"])
    if not path_doc or path_doc.get("status") != "ready":
        return None
    progress = get_or_init_progress(user_email, ctx)
    states = {d["key"]: d for d in compute_day_states(ctx["season_id"], progress)}
    st = states.get(day_key)
    if not st:
        return None
    ticket = None
    if st["state"] in ("completed", "current"):
        for d in path_doc.get("days") or []:
            if d["key"] == day_key:
                ticket = d.get("ticket")
                break
    return {**st, "ticket": ticket}
