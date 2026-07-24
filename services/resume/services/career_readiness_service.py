"""Career Readiness Score — the Compass spine.

ONE honest 0-100 score that synthesizes signals the app already computes (via
overview_service) plus the onboarding goal/level/intensity the user gave us at
sign-up but that was never used downstream.

Philosophy: recognize the *building* of skills, not just applications. A dedicated
"growth" dimension (skills earned + learning) counts toward the score, so a student
doing the hard work sees the number rise — and the weakest dimension always yields
a concrete next step, so nobody is left wondering "what now?".

Deterministic, no LLM, fast — safe on dashboard load. Reuses
overview_service.get_overview() as the single source of cross-surface truth.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

WEIGHTS = {"foundation": 0.20, "skills": 0.18, "applications": 0.18, "interview": 0.17, "growth": 0.15, "proof": 0.12}

# Weakest dimension → the single next best step + which workspace tab to open.
_STEP = {
    "foundation": {"label": "Set up your profile", "cta_tab": "my-resumes",
                   "description": "Upload your résumé and finish onboarding so we can tailor everything to your goal."},
    "skills": {"label": "Close a skill gap", "cta_tab": "learning",
               "description": "Pick one missing skill for your target roles and start a short learning path."},
    "applications": {"label": "Tailor & apply to a matched role", "cta_tab": "jobs",
                     "description": "Tailor your résumé to a strong-match job and apply — quality over quantity beats spray-and-pray."},
    "interview": {"label": "Practice a mock interview", "cta_tab": "interview",
                  "description": "Run one mock interview to get a score and targeted, specific feedback."},
    "growth": {"label": "Start building a skill", "cta_tab": "learning",
               "description": "Begin a learning path in something you want to master — it counts toward your readiness."},
    "proof": {"label": "Show your proof of skill", "cta_tab": "dashboard",
              "description": "Link your GitHub so your real, shipped projects count toward your readiness."},
}


def _db():
    return DBConnect().get_db()


def _clamp(x) -> int:
    return max(0, min(100, int(round(x))))


def _stage(overall: int):
    if overall >= 80:
        return "standout", "Standout candidate"
    if overall >= 60:
        return "ready", "Job-ready"
    if overall >= 35:
        return "building", "Building momentum"
    return "starting", "Getting started"


def _highlights(ov: Dict) -> List[str]:
    """Celebrate the hard work — surfaced prominently so building is recognized."""
    learning = ov.get("learning", {}) or {}
    apps = ov.get("applications", {}) or {}
    interview = ov.get("interview", {}) or {}
    out: List[str] = []
    if learning.get("earned_skills_count"):
        out.append(f"{learning['earned_skills_count']} skill(s) earned by building")
    if learning.get("modules_completed"):
        out.append(f"{learning['modules_completed']} learning module(s) completed")
    if apps.get("offers"):
        out.append(f"{apps['offers']} offer(s)🎉")
    if apps.get("interviewing"):
        out.append(f"{apps['interviewing']} interview(s) in progress")
    if interview.get("attempts"):
        out.append(f"{interview['attempts']} mock answer(s) practiced")
    return out[:4]


def _next_step(dims: List[Dict], target_role) -> Dict:
    weakest = min(dims, key=lambda d: d["score"])
    step = dict(_STEP.get(weakest["key"], _STEP["foundation"]))
    step["dimension"] = weakest["key"]
    if target_role and weakest["key"] in ("skills", "applications", "interview"):
        step["description"] = step["description"].rstrip(".") + f" — for {target_role}."
    return step


def _build_plan(ov: Dict, prefs: Dict, proof_linked: bool) -> List[Dict]:
    """The guided journey: ordered milestones a student completes one at a time,
    each deep-linking to where they do it. `done` is derived from real state, so
    the checklist reflects actual progress — building, recognized step by step."""
    apps = ov.get("applications", {}) or {}
    resume = ov.get("resume", {}) or {}
    learning = ov.get("learning", {}) or {}
    interview = ov.get("interview", {}) or {}
    jobs = ov.get("jobs", {}) or {}
    has_goal = bool(prefs.get("onboarding_complete") and (prefs.get("target_roles") or []))
    return [
        {"key": "resume", "label": "Upload your résumé", "cta_tab": "my-resumes",
         "description": "We build everything from your real experience — start here.",
         "done": bool(resume.get("has_base"))},
        {"key": "goal", "label": "Set your target role", "cta_tab": "profile",
         "description": "Tell us where you're headed so every step is tailored to it.",
         "done": has_goal},
        {"key": "github", "label": "Prove your skills — link GitHub", "cta_tab": "dashboard",
         "description": "Your shipped projects are real proof, recognized in your score.",
         "done": bool(proof_linked)},
        {"key": "tailor", "label": "Tailor your résumé to a role", "cta_tab": "tailor",
         "description": "Match your résumé to a job — honestly, never fabricated.",
         "done": int(apps.get("total", 0) or 0) > 0},
        {"key": "save", "label": "Save 5 matched jobs", "cta_tab": "jobs",
         "description": "Build a focused shortlist — we screen each one for scams.",
         "done": int(jobs.get("saved", 0) or 0) >= 5},
        {"key": "apply", "label": "Apply to your first role", "cta_tab": "applications",
         "description": "Quality over quantity — one strong application beats fifty.",
         "done": int(apps.get("active", 0) or 0) > 0},
        {"key": "skill", "label": "Build one new skill", "cta_tab": "learning",
         "description": "Close a gap for your target role — small steps, real progress.",
         "done": int(learning.get("earned_skills_count", 0) or 0) > 0},
        {"key": "interview", "label": "Practice a mock interview", "cta_tab": "interview",
         "description": "Get a score and specific feedback — build real confidence.",
         "done": int(interview.get("attempts", 0) or 0) > 0},
    ]


def compute_readiness(email: str) -> Dict:
    from services import overview_service
    try:
        ov = overview_service.get_overview(email)
    except Exception as e:
        logger.warning("readiness: overview failed: %s", e)
        ov = {}
    prefs = _db().user_preferences.find_one({"user_email": email}) or {}

    apps = ov.get("applications", {}) or {}
    resume = ov.get("resume", {}) or {}
    learning = ov.get("learning", {}) or {}
    interview = ov.get("interview", {}) or {}
    skills = ov.get("skills", {}) or {}

    target_roles = prefs.get("target_roles") or []
    target_role = target_roles[0] if target_roles else None
    onboarding_complete = bool(prefs.get("onboarding_complete"))
    have_count = int(skills.get("have_count", 0) or 0)
    top_gaps = skills.get("top_gaps", []) or []
    gap_pressure = sum(int(g.get("count", 1) or 1) for g in top_gaps)

    # 1) Foundation — résumé + goal + skills + a linked professional presence.
    foundation = 0
    if resume.get("has_base"):
        foundation += 40
    if onboarding_complete:
        foundation += 15
    if target_role:
        foundation += 12
    foundation += min(have_count, 5) * 4
    if prefs.get("linkedin_url"):
        foundation += 13  # linking LinkedIn (via the audit) completes the profile
    foundation = _clamp(foundation)

    # 2) Skills — coverage vs the gaps for their target roles.
    skill_score = _clamp(45 + min(have_count, 12) * 4 - min(gap_pressure, 12) * 3)

    # 3) Applications — pipeline activity (offers/interviews weigh most).
    total = int(apps.get("total", 0) or 0)
    active = int(apps.get("active", 0) or 0)
    interviewing = int(apps.get("interviewing", 0) or 0)
    offers = int(apps.get("offers", 0) or 0)
    app_score = _clamp(min(total, 10) * 7 + active * 4 + interviewing * 6 + offers * 15)

    # 4) Interview — mock-interview performance.
    attempts = int(interview.get("attempts", 0) or 0)
    avg = interview.get("avg_score")
    has_avg = attempts and isinstance(avg, (int, float))
    interview_score = _clamp(avg) if has_avg else (40 if attempts else 0)

    # 5) Growth — the recognition dimension: skill-building counts.
    earned = int(learning.get("earned_skills_count", 0) or 0)
    modules = int(learning.get("modules_completed", 0) or 0)
    active_paths = int(learning.get("active_paths", 0) or 0)
    growth = _clamp(earned * 14 + modules * 5 + active_paths * 10)

    # 6) Proof — real, shipped work. GitHub is the strongest signal that a student
    # actually built things; fall back to résumé projects so non-GitHub users aren't zeroed.
    proof = None
    try:
        from services import github_profile_service
        proof = github_profile_service.proof_score_for(email)
    except Exception:
        proof = None
    if proof is not None:
        proof_score = _clamp(proof)
        proof_summary = "GitHub linked — real projects count"
    else:
        try:
            # Resume Platform v3: canonical read path (active resume asset).
            from services import resume_access
            rdoc = resume_access.get_active_structured(email) or {}
            nproj = len(((rdoc.get("structured") or {}).get("projects") or []))
        except Exception:
            nproj = 0
        proof_score = _clamp(min(60, nproj * 20))
        proof_summary = f"{nproj} résumé project(s) · link GitHub for more" if nproj else "Link GitHub to prove your skills"

    dims = [
        {"key": "foundation", "label": "Profile foundation", "score": foundation,
         "summary": "Résumé, goal & skills set up"},
        {"key": "skills", "label": "Skill match", "score": skill_score,
         "summary": f"{have_count} skills · {len(top_gaps)} gap(s) to target"},
        {"key": "applications", "label": "Application momentum", "score": app_score,
         "summary": f"{total} tailored · {active} active · {offers} offer(s)"},
        {"key": "interview", "label": "Interview readiness", "score": interview_score,
         "summary": (f"{attempts} mock answers · avg {int(avg)}" if has_avg else (f"{attempts} mock answers" if attempts else "Not started yet"))},
        {"key": "growth", "label": "Skills you're building", "score": growth,
         "summary": f"{earned} earned · {modules} modules · {active_paths} active path(s)"},
        {"key": "proof", "label": "Proof of skill", "score": proof_score,
         "summary": proof_summary},
    ]

    overall = _clamp(sum(d["score"] * WEIGHTS[d["key"]] for d in dims))
    band, level = _stage(overall)

    return {
        "overall": overall,
        "band": band,
        "level": level,
        "target_role": target_role,
        "intensity": prefs.get("intensity"),
        "experience_level": prefs.get("experience_level"),
        "onboarding_complete": onboarding_complete,
        "dimensions": dims,
        "next_step": _next_step(dims, target_role),
        "build_plan": _build_plan(ov, prefs, proof is not None),
        "highlights": _highlights(ov),
        "computed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
