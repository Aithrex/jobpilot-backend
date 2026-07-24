"""Learning Hub — the universal curriculum spine (pure data, no I/O).

The Sprint-based Learning Hub is built on ONE versioned 14-day objective spine
shared across every career path. The AI generation layer
(`sprint_generation_service`) transforms each generic day objective into a
realistic, company-style engineering ticket TAILORED to the user's canonical
role + difficulty, while preserving this exact progression for everyone.

Bump `CURRICULUM_VERSION` to force regeneration of all cached paths. Because the
version is embedded in each path's cache key, a bump means users start a fresh
sprint on the new curriculum (old progress is preserved under the old key).

Adding a new SEASON (30/60/90-day programs) later = adding a new SPINE list +
SEASONS entry; nothing else in the stack hardcodes 14.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Dict, List

# Bump to invalidate + regenerate every cached path.
CURRICULUM_VERSION = 1

# ---------------------------------------------------------------------------
# Incremental rollout gate. Only days 1..PUBLISHED_THROUGH_DAY have real,
# generated content; the rest render as "coming soon" in the timeline. To
# release more of the curriculum later, bump this number (and, if the ticket
# content changes, CURRICULUM_VERSION) — NO other code changes needed. The
# unlock/state machine, XP, achievements, and analytics already handle the
# full 14-day journey; this only limits which days are populated.
# ---------------------------------------------------------------------------
PUBLISHED_THROUGH_DAY = 1


def is_published(day: int) -> bool:
    """True if this day number has real content in the current version."""
    try:
        return int(day) <= PUBLISHED_THROUGH_DAY
    except (TypeError, ValueError):
        return False

# ---------------------------------------------------------------------------
# The universal objective spine. `key` is STABLE across versions so progress
# (keyed by day key, not array index) survives a same-length regeneration.
# `theme` drives XP weighting + the "projects built" analytics count.
# ---------------------------------------------------------------------------
SPINE: Dict[str, List[Dict]] = {
    "sprint-14": [
        {"day": 1,  "key": "env_setup",         "objective": "Environment & project scaffolding",        "theme": "setup"},
        {"day": 2,  "key": "core_impl",         "objective": "Core feature implementation",              "theme": "build"},
        {"day": 3,  "key": "data_layer",        "objective": "Data modeling & persistence",              "theme": "build"},
        {"day": 4,  "key": "rest_apis",         "objective": "REST API design & endpoints",              "theme": "build"},
        {"day": 5,  "key": "auth_security",     "objective": "AuthN/AuthZ & security hardening",         "theme": "quality"},
        {"day": 6,  "key": "testing",           "objective": "Unit & integration testing",               "theme": "quality"},
        {"day": 7,  "key": "optimization",      "objective": "Performance profiling & optimization",     "theme": "quality"},
        {"day": 8,  "key": "containerize",      "objective": "Containerization & packaging",             "theme": "ops"},
        {"day": 9,  "key": "ci_cd",             "objective": "CI/CD pipeline",                           "theme": "ops"},
        {"day": 10, "key": "monitoring",        "objective": "Observability & monitoring",               "theme": "ops"},
        {"day": 11, "key": "debug_distributed", "objective": "Debugging a distributed-systems incident", "theme": "advanced"},
        {"day": 12, "key": "scale",             "objective": "Scaling & resilience",                     "theme": "advanced"},
        {"day": 13, "key": "review",            "objective": "Code review & refactor",                   "theme": "advanced"},
        {"day": 14, "key": "final_delivery",    "objective": "Final delivery & demo",                    "theme": "capstone"},
    ],
}

SEASONS: Dict[str, Dict] = {
    "sprint-14": {
        "season_id": "sprint-14",
        "label": "14-Day Engineering Sprint",
        "length_days": 14,
        "spine_key": "sprint-14",
        "curriculum_version": CURRICULUM_VERSION,
    },
    # Future: "sprint-30", "sprint-60", "sprint-90" — same shape, longer SPINE.
}
DEFAULT_SEASON = "sprint-14"

# ---------------------------------------------------------------------------
# XP + levels
# ---------------------------------------------------------------------------
XP_BY_THEME = {
    "setup": 50,
    "build": 50,
    "quality": 60,
    "ops": 60,
    "advanced": 75,
    "capstone": 150,
}
DEFAULT_DAY_XP = 50

# Triangular-ish thresholds so early levels feel fast. Index == level reached.
LEVEL_THRESHOLDS = [0, 100, 250, 450, 700, 1000, 1400, 1900, 2500, 3200, 4000, 5000]


def spine_for_season(season_id: str) -> List[Dict]:
    season = SEASONS.get(season_id) or SEASONS[DEFAULT_SEASON]
    return SPINE.get(season["spine_key"]) or SPINE["sprint-14"]


def day_meta(season_id: str, day_key: str) -> Dict:
    for sd in spine_for_season(season_id):
        if sd["key"] == day_key:
            return sd
    return {}


def xp_for_day(season_id: str, day_key: str) -> int:
    meta = day_meta(season_id, day_key)
    return XP_BY_THEME.get(meta.get("theme"), DEFAULT_DAY_XP)


def level_for_xp(xp: int) -> int:
    """1-based level. xp=0 -> level 1."""
    return max(1, bisect_right(LEVEL_THRESHOLDS, int(xp or 0)))


def next_level_xp(xp: int) -> int:
    """XP threshold to reach the next level (or the current xp if maxed)."""
    lvl = level_for_xp(xp)
    if lvl < len(LEVEL_THRESHOLDS):
        return LEVEL_THRESHOLDS[lvl]
    return int(xp or 0)
