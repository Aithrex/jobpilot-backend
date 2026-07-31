"""Learning Hub — canonical career-path registry + free-text mapping.

This is the EXTENSION POINT for supporting new career paths: add one entry to
`CANONICAL_PATHS` and everything else (generation, caching, UI) works. No
hand-authored curricula — the universal spine (`sprint_curriculum`) plus the AI
generator adapt to whatever role is registered here.

Mapping is deterministic (no LLM): it runs on every hub load and must be free,
instant, and stable so the cache key `(role, difficulty, version, season)` is
reproducible.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# code -> ticket prefix used for deterministic ticket numbers (e.g. API-204).
CANONICAL_PATHS: Dict[str, Dict] = {
    "software_engineer":  {"label": "Software Engineer",           "code": "ENG",  "aliases": ["software engineer", "software developer", "swe", "programmer", "coder"]},
    "backend_engineer":   {"label": "Backend Engineer",            "code": "API",  "aliases": ["backend", "back end", "back-end", "api engineer", "server engineer", "server-side"]},
    "frontend_engineer":  {"label": "Frontend Engineer",           "code": "UI",   "aliases": ["frontend", "front end", "front-end", "ui engineer", "react", "web developer", "javascript engineer"]},
    "fullstack_engineer": {"label": "Full Stack Engineer",         "code": "FS",   "aliases": ["full stack", "fullstack", "full-stack", "mern", "mean"]},
    "cloud_engineer":     {"label": "Cloud Engineer",              "code": "CLD",  "aliases": ["cloud", "aws", "azure", "gcp", "cloud architect"]},
    "devops_engineer":    {"label": "DevOps Engineer",             "code": "OPS",  "aliases": ["devops", "dev ops", "platform engineer", "infrastructure", "infra"]},
    "data_engineer":      {"label": "Data Engineer",               "code": "DATA", "aliases": ["data engineer", "etl", "data pipeline", "spark", "big data"]},
    "ai_ml_engineer":     {"label": "AI/ML Engineer",              "code": "ML",   "aliases": ["ml", "machine learning", "ai engineer", "ai/ml", "mlops", "deep learning", "data scientist"]},
    "cybersecurity":      {"label": "Cybersecurity Specialist",    "code": "SEC",  "aliases": ["security", "cyber", "cybersecurity", "infosec", "appsec", "pentest", "security engineer"]},
    "qa_engineer":        {"label": "QA Engineer",                 "code": "QA",   "aliases": ["qa", "quality assurance", "sdet", "test engineer", "automation tester"]},
    "mobile_engineer":    {"label": "Mobile Developer",            "code": "MOB",  "aliases": ["mobile", "ios", "android", "flutter", "react native", "swift", "kotlin"]},
    "sre":                {"label": "Site Reliability Engineer",   "code": "SRE",  "aliases": ["sre", "site reliability", "reliability engineer"]},
    "product_manager":    {"label": "Product Manager",             "code": "PM",   "aliases": ["product manager", "product owner", "pm", "product management"]},
    "generalist":         {"label": "Software Engineer",           "code": "ENG",  "aliases": []},
}
DEFAULT_PATH = "generalist"

DIFFICULTY_TIERS = ("junior", "mid", "senior")
DEFAULT_DIFFICULTY = "mid"

DIFFICULTY_LABELS = {"junior": "Junior", "mid": "Mid-level", "senior": "Senior"}

_JUNIOR_HINTS = ("intern", "entry", "junior", "grad", "graduate", "trainee", "associate", "student", "0-1", "0-2")
_SENIOR_HINTS = ("senior", "staff", "principal", "lead", "architect", "manager", "head", "director", "expert", "5+", "6+", "7+", "8+", "10+")


def all_paths() -> List[Dict]:
    """Registry as a list for admin role-switcher dropdowns."""
    return [
        {"key": k, "label": v["label"]}
        for k, v in CANONICAL_PATHS.items()
        if k != "generalist"
    ]


def path_meta(role_key: str) -> Dict:
    return CANONICAL_PATHS.get(role_key) or CANONICAL_PATHS[DEFAULT_PATH]


def is_valid_role(role_key: str) -> bool:
    return role_key in CANONICAL_PATHS


def map_role(free_text: Optional[str]) -> str:
    """Map an arbitrary role/title string to a canonical path key.

    Longest alias match wins so "senior backend engineer" -> backend_engineer
    rather than the shorter "engineer" fallback. Deterministic, no LLM.
    """
    if not free_text:
        return DEFAULT_PATH
    text = str(free_text).lower().strip()
    if text in CANONICAL_PATHS:
        return text
    best_key, best_len = DEFAULT_PATH, 0
    for key, meta in CANONICAL_PATHS.items():
        for alias in meta["aliases"]:
            if alias in text and len(alias) > best_len:
                best_key, best_len = key, len(alias)
    return best_key


def map_difficulty(experience_level: Optional[str] = "",
                   seniority: Optional[str] = "",
                   role_text: Optional[str] = "") -> str:
    """Map free-text experience signals to a difficulty tier."""
    blob = " ".join([str(experience_level or ""), str(seniority or ""), str(role_text or "")]).lower()
    if any(h in blob for h in _SENIOR_HINTS):
        return "senior"
    if any(h in blob for h in _JUNIOR_HINTS):
        return "junior"
    return DEFAULT_DIFFICULTY


def resolve_for_user(user_doc: Optional[Dict], prefs_doc: Optional[Dict]) -> Tuple[str, str]:
    """Resolve (canonical_role_key, difficulty) purely from profile data.

    Priority for role: preferences.target_roles[0] -> users.role.
    Difficulty: preferences.experience_level / seniority (+ role text hints).
    """
    user_doc = user_doc or {}
    prefs_doc = prefs_doc or {}

    target_roles = prefs_doc.get("target_roles") or []
    role_text = (target_roles[0] if target_roles else None) or user_doc.get("role") or ""
    role_key = map_role(role_text)

    difficulty = map_difficulty(
        experience_level=prefs_doc.get("experience_level"),
        seniority=prefs_doc.get("seniority"),
        role_text=user_doc.get("role") or role_text,
    )
    return role_key, difficulty
