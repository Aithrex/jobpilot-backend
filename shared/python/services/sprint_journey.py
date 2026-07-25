"""Learning Hub — the multi-sprint campaign (linear chapter progression).

A "journey" is the ordered sequence of story chapters (seasons). Chapters run in
order: finish one to unlock the next. This module owns the cross-sprint gating —
which chapter is locked / active / completed for a given user.

State is DERIVED, not stored: a chapter is "complete" when the user's per-season
`sprint_progress` doc (keyed by that season's `path_key`) has completed every
PUBLISHED day of the chapter. "Complete = all published days" (not all
`length_days`) so the journey works under the incremental-rollout gate — you can
only be asked to finish what's actually released.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from utils.db_connect import DBConnect
from services import career_paths
from services import sprint_curriculum as curriculum
from services import sprint_challenge_service as challenge

logger = logging.getLogger(__name__)


def _db():
    return DBConnect().get_db()


def _published_day_count(season_id: str) -> int:
    return sum(1 for sd in curriculum.spine_for_season(season_id)
               if curriculum.is_published(sd["day"], season_id))


def _completed_count(user_email: str, role_key: str, difficulty: str, season_id: str) -> int:
    path_key = challenge.build_path_key(role_key, difficulty, season_id)
    doc = _db().sprint_progress.find_one(
        {"user_email": user_email, "path_key": path_key},
        {"completed_count": 1, "day_status": 1},
    )
    if not doc:
        return 0
    cc = doc.get("completed_count")
    if cc is not None:
        return int(cc)
    # Fallback: count completed_at stamps directly.
    return sum(1 for v in (doc.get("day_status") or {}).values() if (v or {}).get("completed_at"))


def is_complete(user_email: str, role_key: str, difficulty: str, season_id: str) -> bool:
    pub = _published_day_count(season_id)
    return pub > 0 and _completed_count(user_email, role_key, difficulty, season_id) >= pub


def is_unlocked(user_email: str, role_key: str, difficulty: str, season_id: str) -> bool:
    """A chapter is unlocked if it's the first, or the previous chapter is complete."""
    seasons = curriculum.ordered_seasons()
    idx = next((i for i, s in enumerate(seasons) if s["season_id"] == season_id), None)
    if idx is None:
        return False
    if idx == 0:
        return True
    prev = seasons[idx - 1]
    return is_complete(user_email, role_key, difficulty, prev["season_id"])


def active_season(user_email: str, role_key: str, difficulty: str) -> str:
    """The user's current chapter: the first unlocked-and-incomplete one. If every
    chapter is complete, the last chapter (so the UI has something to show)."""
    seasons = curriculum.ordered_seasons()
    for s in seasons:
        sid = s["season_id"]
        if is_unlocked(user_email, role_key, difficulty, sid) and \
                not is_complete(user_email, role_key, difficulty, sid):
            return sid
    return seasons[-1]["season_id"] if seasons else curriculum.DEFAULT_SEASON


def journey(user_email: str, role_key: str, difficulty: str) -> Dict:
    """The full campaign map with per-user state — drives the chapter-select UI."""
    role_label = career_paths.path_meta(role_key)["label"]
    active = active_season(user_email, role_key, difficulty)
    seasons_out: List[Dict] = []
    for s in curriculum.ordered_seasons():
        sid = s["season_id"]
        unlocked = is_unlocked(user_email, role_key, difficulty, sid)
        complete = is_complete(user_email, role_key, difficulty, sid)
        if complete:
            state = "completed"
        elif not unlocked:
            state = "locked"
        elif sid == active:
            state = "active"
        else:
            state = "available"
        seasons_out.append({
            "season_id": sid,
            "order": s.get("order"),
            "title": s.get("title") or s.get("label"),
            "premise": (s.get("premise") or "").replace("{role}", role_label),
            "length_days": s.get("length_days"),
            "published_days": _published_day_count(sid),
            "completed_count": _completed_count(user_email, role_key, difficulty, sid),
            "state": state,
        })

    # Teaser chapters — no content yet, never startable. They convey that the
    # series continues ("more chapters ahead").
    for c in curriculum.COMING_SOON_CHAPTERS:
        seasons_out.append({
            "season_id": f"coming-soon-{c.get('order')}",
            "order": c.get("order"),
            "title": c.get("title"),
            "premise": (c.get("premise") or "").replace("{role}", role_label),
            "length_days": c.get("length_days"),
            "published_days": 0,
            "completed_count": 0,
            "state": "coming_soon",
        })

    seasons_out.sort(key=lambda s: s.get("order") or 999)
    return {"seasons": seasons_out, "active_season_id": active}
