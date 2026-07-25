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


def is_published(day: int, season_id: str = None) -> bool:
    """True if this day number has real content for the given season.

    Publishing is now PER-SEASON: a season may declare its own
    `published_through_day` (e.g. a fully-authored Sprint 2 ships all its days at
    once) and falls back to the global `PUBLISHED_THROUGH_DAY` when it doesn't.
    (SEASONS/DEFAULT_SEASON are resolved at call time — they're defined below.)
    """
    try:
        d = int(day)
    except (TypeError, ValueError):
        return False
    season = SEASONS.get(season_id or DEFAULT_SEASON) or {}
    gate = season.get("published_through_day", PUBLISHED_THROUGH_DAY)
    return d <= gate

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
    # Chapter 2 — "Ship It": you own your first real feature end-to-end, from
    # design through a production incident to launch. 10 story beats (deliberately
    # shorter than Chapter 1 to prove the variable-length journey).
    "sprint-2": [
        {"day": 1,  "key": "s2_kickoff",     "objective": "Feature kickoff & design doc",              "theme": "setup"},
        {"day": 2,  "key": "s2_data_model",  "objective": "Data model & schema for the feature",       "theme": "build"},
        {"day": 3,  "key": "s2_build_api",   "objective": "Build the feature's API/service layer",     "theme": "build"},
        {"day": 4,  "key": "s2_build_ui",    "objective": "Build the user-facing surface",             "theme": "build"},
        {"day": 5,  "key": "s2_tests",       "objective": "Write the test suite for the feature",      "theme": "quality"},
        {"day": 6,  "key": "s2_review",      "objective": "Open a PR & address code-review feedback",   "theme": "quality"},
        {"day": 7,  "key": "s2_staging",     "objective": "Deploy to staging behind a flag",           "theme": "ops"},
        {"day": 8,  "key": "s2_incident",    "objective": "On-call: diagnose a production incident",    "theme": "advanced"},
        {"day": 9,  "key": "s2_hotfix",      "objective": "Ship the hotfix & write the postmortem",     "theme": "advanced"},
        {"day": 10, "key": "s2_launch",      "objective": "Launch to 100% & run the retro",            "theme": "capstone"},
    ],
}

# A season = one narrative "chapter". `order` drives the linear campaign; `title`
# + `premise` frame the story (woven into ticket generation and shown on the
# journey map). `published_through_day` gates how many days ship real content
# (per-season; falls back to the global PUBLISHED_THROUGH_DAY).
SEASONS: Dict[str, Dict] = {
    "sprint-14": {
        "season_id": "sprint-14",
        "label": "14-Day Engineering Sprint",
        "length_days": 14,
        "spine_key": "sprint-14",
        "curriculum_version": CURRICULUM_VERSION,
        "order": 1,
        "title": "Chapter 1 · Onboarding",
        "premise": (
            "You've just joined Northwind, a fast-moving product company, as a {role}. "
            "Your first two weeks: get productive, ship your first commits, and earn the "
            "team's trust one ticket at a time."
        ),
        "published_through_day": 14,   # fully authored — all 14 days live, so Chapter 2
                                       # unlocks only after the WHOLE chapter is completed
    },
    "sprint-2": {
        "season_id": "sprint-2",
        "label": "Ship It",
        "length_days": 10,
        "spine_key": "sprint-2",
        "curriculum_version": CURRICULUM_VERSION,
        "order": 2,
        "title": "Chapter 2 · Ship It",
        "premise": (
            "You've earned your team's trust at Northwind. Now you own your first real "
            "feature end-to-end as a {role} — design it, build it, test it, ship it to "
            "staging, survive a production incident, and launch it to everyone."
        ),
        "published_through_day": 10,   # fully authored — all 10 days live
    },
    # Future chapters: add a SPINE list + a SEASONS entry with the next `order`.
}
DEFAULT_SEASON = "sprint-14"

# Teaser chapters shown on the journey roadmap to convey the ongoing series.
# These have NO spine and NO content — they are intentionally NOT in SEASONS, so
# they can never be started (the /start route only accepts ids present in
# SEASONS). Promote one to a real chapter later by adding its SPINE + SEASONS
# entry. Order continues after the real seasons.
COMING_SOON_CHAPTERS: List[Dict] = [
    {"order": 3, "title": "Chapter 3 · The Scale-Up", "length_days": 12,
     "premise": "Northwind just landed its biggest customer and traffic 10x'd overnight. "
                "Keep the platform standing as a {role} — caching, queues, and hard "
                "scaling calls under real pressure."},
    {"order": 4, "title": "Chapter 4 · On-Call Rotation", "length_days": 7,
     "premise": "You've joined the on-call rotation. A week of real incidents, pages, and "
                "postmortems — learn to stay calm, debug live systems, and make them "
                "more resilient afterward."},
    {"order": 5, "title": "Chapter 5 · The Big Refactor", "length_days": 14,
     "premise": "Years of shortcuts are slowing the whole team down. Lead a careful, "
                "test-guarded refactor of a critical subsystem without breaking a thing."},
]

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


def season_meta(season_id: str) -> Dict:
    """The season's config dict (falls back to the default season)."""
    return SEASONS.get(season_id) or SEASONS[DEFAULT_SEASON]


def ordered_seasons() -> List[Dict]:
    """All seasons in campaign order — the linear chapter sequence."""
    return sorted(SEASONS.values(), key=lambda s: s.get("order", 999))


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
