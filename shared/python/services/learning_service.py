"""Learning Hub (Pillar B) — AI-generated, end-to-end study plans for any topic,
plus the skill→job loop.

Flagship differentiator: finish a module and we (1) infer the concrete skill it
proves, (2) stage it on the user's profile (never silently editing their resume),
and (3) surface the saved jobs that skill just unlocked.

Built on the same Claude-on-Bedrock provider + async-job substrate as resume
tailoring. Plan generation is a PRO async job; module completion + quiz are
lightweight/sync.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from utils.db_connect import DBConnect
from services.claude_client import claude_json, CLAUDE_PRO, CLAUDE_FLASH

logger = logging.getLogger(__name__)

XP_PER_MODULE = 50

PLAN_SCHEMA = {
    "title": str,
    "summary": str,
    "target_skills": [str],
    "modules": [{
        "week": int,
        "title": str,
        "objective": str,
        "concepts": [str],
        "practice_task": str,
        "resource_queries": [str],
        "checkpoint": str,
        "inferred_skill": str,
    }],
    "capstone": {"title": str, "description": str},
}

# Sprint format: one Jira-style ticket per day, each demanding pasted proof of
# work. Shape is skill-agnostic — the skill/role/progression come from the
# caller (see TYPESCRIPT_SPRINT) so new tracks are data, not new code.
SPRINT_SCHEMA = {
    "title": str,
    "summary": str,
    "target_skills": [str],
    "days": [{
        "day": int,
        "title": str,
        "business_context": str,
        "task_instructions": [str],
        "acceptance_criteria": [str],
        "required_proof_instructions": str,
    }],
}

TYPESCRIPT_SPRINT = {
    "skill": "TypeScript",
    "role": "Full-Stack / Backend Software Engineer",
    "baseline": "Knows JavaScript, Node.js, and basic programming concepts.",
    "progression": (
        "- Days 1-3 (Core Foundations): Strict tsconfig setup, interfaces vs. type aliases, "
        "optionals, unions, basic generics, and compiler error resolution.\n"
        "- Days 4-7 (Real-World Application): Typing async functions, Promises, Express/REST "
        "handlers, Zod schema validation, and utility types (Pick, Omit, Partial, Record).\n"
        "- Days 8-10 (Advanced & Production): Generic constraints "
        "(`<T extends Record<string, any>>`), custom Type Guards (`is` predicates), CI/CD "
        "type-checking (`tsc --noEmit`), and converting legacy JS files to strict TS."
    ),
}


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _iso(v):
    if not hasattr(v, "isoformat"):
        return v
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


# ---------------------------------------------------------------------------
# Generation (runs inside the async job)
# ---------------------------------------------------------------------------
def generate_plan(topic: str, target_role: Optional[str], current_skills: List[str],
                  weeks: int = 6, include_capstone: bool = False) -> Dict:
    """Produce a structured, grounded study plan via the PRO model."""
    skills_line = ", ".join(current_skills[:30]) or "(none provided)"
    capstone_line = (
        "Also include a `capstone` (title + description): a portfolio-worthy project that "
        "ties the whole plan together and would impress an interviewer."
        if include_capstone else
        "Do not include a capstone."
    )
    prompt = f"""You are a senior engineer and mentor designing a focused, end-to-end study plan.

Learner wants to master: "{topic}"
Target role (tailor depth/examples to this if given): {target_role or "general career growth"}
Skills they already have (don't re-teach these; build on them): {skills_line}

Design a {weeks}-week plan. For EACH week produce one module with:
- week (1..{weeks})
- title
- objective (one sentence on what they'll be able to do)
- concepts (3-6 specific things to learn)
- practice_task (a concrete, hands-on task that PROVES the skill — buildable, not "read about X")
- resource_queries (2-4 search queries they can paste into Google/YouTube to find current material)
- checkpoint (how they know they've got it)
- inferred_skill (the SINGLE resume-grade skill mastering this module proves — short, e.g. "Docker", "REST API design", "A/B testing")

Make it progressive (each week builds on the last), practical, and honest — only real, learnable skills.
{capstone_line}
Also return a short `title` for the whole plan, a one-paragraph `summary`, and `target_skills` (the full set of skills the plan builds).

Return STRICT JSON matching the schema."""
    plan = claude_json(prompt=prompt, model=CLAUDE_PRO, max_tokens=8000, temperature=0.4, schema=PLAN_SCHEMA)
    return plan or {}


def generate_sprint(skill: str, role: str, baseline: str, progression: str,
                    days: int = 10) -> Dict:
    """Produce a day-by-day engineering sprint of Jira-style tickets.

    Unlike `generate_plan` (weekly study modules), every day here is a ticket
    with a business justification and an explicit proof-of-work demand, so
    completion can be evidenced rather than self-reported.
    """
    prompt = f"""You are a Staff Technical Lead at an enterprise tech company. A software engineer on your team has a skill gap in {skill} and needs a hands-on, progressive {days}-day engineering sprint to reach production competency.

Generate a complete {days}-day {skill} sprint where every day simulates a real Jira ticket.

### Context:
- Target Skill: {skill}
- Target Role: {role}
- Learner Baseline: {baseline}

### Progression Plan:
{progression}

### Requirements for EACH Day (1 through {days}):
1. day: Integer (1 to {days}).
2. title: Short Jira-style ticket title (e.g., "TS-101: Configure Strict Compiler & User Service Contracts").
3. business_context: 2-3 sentences explaining the real-world business reason for this work (e.g., "Production crashes occurred due to unchecked undefined user properties. We need strict compile-time types to prevent runtime panics.").
4. task_instructions: Bulleted, step-by-step instructions of what code/files to write or refactor locally on their machine.
5. acceptance_criteria: 3 to 4 testable bullet points defining done.
6. required_proof_instructions: Explicit instructions on EXACTLY what files, terminal output, or compiler logs the engineer must copy/paste into the platform as proof of work.

Also return a short `title` for the whole sprint, a one-paragraph `summary`, and `target_skills` (the full set of skills the sprint builds).

Return STRICT JSON matching the schema. Do not wrap the JSON in markdown backticks or add introductory text."""
    sprint = claude_json(
        prompt=prompt,
        model=CLAUDE_PRO,
        max_tokens=8000,
        temperature=0.3,
        schema=SPRINT_SCHEMA,
    )
    return sprint or {}


def generate_typescript_sprint() -> Dict:
    """The TypeScript track — thin wrapper over `generate_sprint`."""
    return generate_sprint(**TYPESCRIPT_SPRINT)


def build_plan_job(payload: Dict) -> Dict:
    """Entry point invoked from resume_service._process_job for job_type='learning_plan'."""
    user_email = payload.get("user_email", "")
    topic = (payload.get("topic") or "").strip()
    target_role = payload.get("target_role")
    current_skills = payload.get("current_skills") or []
    weeks = int(payload.get("weeks") or 6)
    include_capstone = bool(payload.get("include_capstone"))

    plan = generate_plan(topic, target_role, current_skills, weeks=weeks, include_capstone=include_capstone)
    modules = plan.get("modules") or []
    if not modules:
        raise ValueError("Learning plan generation returned no modules")

    path_id = str(uuid.uuid4())
    doc = {
        "path_id": path_id,
        "user_email": user_email,
        "topic": topic,
        "title": plan.get("title") or f"Master {topic}",
        "summary": plan.get("summary", ""),
        "target_role": target_role,
        "modules": modules,
        "target_skills": plan.get("target_skills") or [],
        "capstone": plan.get("capstone"),
        "status": "active",
        "created_at": _now(),
        "updated_at": _now(),
    }
    _db().learning_paths.insert_one(doc)
    _db().learning_progress.update_one(
        {"user_email": user_email, "path_id": path_id},
        {"$setOnInsert": {
            "user_email": user_email, "path_id": path_id,
            "completed_step_ids": [], "acquired_skills": [], "xp_earned": 0,
            "quiz_results": [], "created_at": _now(),
        }},
        upsert=True,
    )
    return {"path_id": path_id, "path": _serialize(doc)}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _serialize(doc: Dict) -> Dict:
    doc = dict(doc)
    doc.pop("_id", None)
    for k in ("created_at", "updated_at"):
        if k in doc:
            doc[k] = _iso(doc[k])
    return doc


def count_active_paths(user_email: str) -> int:
    return _db().learning_paths.count_documents({"user_email": user_email, "status": "active"})


def list_paths(user_email: str) -> List[Dict]:
    paths = list(_db().learning_paths.find({"user_email": user_email}).sort("created_at", -1).limit(100))
    progress = {p["path_id"]: p for p in _db().learning_progress.find({"user_email": user_email})}
    out = []
    for p in paths:
        prog = progress.get(p["path_id"], {})
        done = set(prog.get("completed_step_ids", []))
        total = len(p.get("modules", []))
        out.append({
            **_serialize(p),
            "completed_count": len(done),
            "total_modules": total,
            "xp_earned": prog.get("xp_earned", 0),
            "percent": round(100 * len(done) / total) if total else 0,
        })
    return out


def get_path(user_email: str, path_id: str) -> Optional[Dict]:
    p = _db().learning_paths.find_one({"user_email": user_email, "path_id": path_id})
    if not p:
        return None
    prog = _db().learning_progress.find_one({"user_email": user_email, "path_id": path_id}) or {}
    return {
        "path": _serialize(p),
        "progress": {
            "completed_step_ids": prog.get("completed_step_ids", []),
            "acquired_skills": prog.get("acquired_skills", []),
            "xp_earned": prog.get("xp_earned", 0),
            "quiz_results": prog.get("quiz_results", []),
        },
    }


def delete_path(user_email: str, path_id: str) -> bool:
    res = _db().learning_paths.update_one(
        {"user_email": user_email, "path_id": path_id},
        {"$set": {"status": "archived", "updated_at": _now()}},
    )
    return res.modified_count > 0


# ---------------------------------------------------------------------------
# Module completion + skill→job loop
# ---------------------------------------------------------------------------
def complete_module(user_email: str, path_id: str, week: int) -> Dict:
    path = _db().learning_paths.find_one({"user_email": user_email, "path_id": path_id})
    if not path:
        raise ValueError("Learning path not found")
    module = next((m for m in path.get("modules", []) if int(m.get("week", -1)) == int(week)), None)
    if not module:
        raise ValueError("Module not found")

    step_id = f"week-{week}"
    inferred = (module.get("inferred_skill") or "").strip()

    _db().learning_progress.update_one(
        {"user_email": user_email, "path_id": path_id},
        {
            "$addToSet": {"completed_step_ids": step_id, **({"acquired_skills": inferred} if inferred else {})},
            "$inc": {"xp_earned": XP_PER_MODULE},
            "$setOnInsert": {"user_email": user_email, "path_id": path_id, "created_at": _now()},
        },
        upsert=True,
    )

    staged = False
    if inferred and _is_grounded_skill(inferred):
        # Stage on the PROFILE, never silently into user_resumes. The user
        # explicitly adds earned skills to a resume from the UI.
        _db().users.update_one(
            {"email": user_email},
            {"$addToSet": {"earned_skills": {
                "skill": inferred, "path_id": path_id, "week": int(week),
                "proof": module.get("title", ""), "earned_at": _now(),
            }}},
        )
        staged = True

    unlocked = _jobs_unlocked_by_skill(user_email, inferred) if inferred else []

    # Recompute completion → maybe mark path completed.
    prog = _db().learning_progress.find_one({"user_email": user_email, "path_id": path_id}) or {}
    if len(set(prog.get("completed_step_ids", []))) >= len(path.get("modules", [])):
        _db().learning_paths.update_one(
            {"user_email": user_email, "path_id": path_id},
            {"$set": {"status": "completed", "updated_at": _now()}},
        )

    return {
        "earned_skill": inferred or None,
        "staged_for_resume": staged,
        "unlocked_jobs": unlocked,
        "xp_earned": prog.get("xp_earned", XP_PER_MODULE),
    }


def _is_grounded_skill(skill: str) -> bool:
    """Cheap integrity gate — keep obviously-bogus 'skills' out of the profile."""
    s = (skill or "").strip()
    return 1 < len(s) <= 60 and not s.lower().startswith(("learn ", "understand ", "study "))


def _jobs_unlocked_by_skill(user_email: str, skill: str, limit: int = 5) -> List[Dict]:
    """Skill→job loop (no-LLM): saved jobs that call for this skill.

    Reuses saved_jobs the user already collected. We surface roles whose
    title/description/required-skills mention the just-earned skill so the
    learning loop closes back into the application funnel.
    """
    if not skill:
        return []
    s = skill.lower()
    out: List[Dict] = []
    try:
        for sj in _db().saved_jobs.find({"user_email": user_email}).limit(200):
            job = sj.get("job") or {}
            hay = " ".join([
                str(job.get("title", "")), str(job.get("company", "")),
                str(job.get("description", ""))[:1500],
                " ".join(job.get("required_skills", []) or []),
                " ".join(job.get("missing_top", []) or []),
            ]).lower()
            if s in hay:
                out.append({
                    "id": sj.get("job", {}).get("id") or str(sj.get("_id")),
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "url": job.get("url") or job.get("apply_url"),
                    "status": sj.get("status"),
                })
            if len(out) >= limit:
                break
    except Exception as e:
        logger.warning("skill→job lookup failed for %s: %s", user_email, e)
    return out


# ---------------------------------------------------------------------------
# Quiz
# ---------------------------------------------------------------------------
QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer_index": {"type": "integer"},
                    "explanation": {"type": "string"},
                },
            },
        }
    },
}


def generate_quiz(user_email: str, path_id: str, week: int) -> Dict:
    path = _db().learning_paths.find_one({"user_email": user_email, "path_id": path_id})
    if not path:
        raise ValueError("Learning path not found")
    module = next((m for m in path.get("modules", []) if int(m.get("week", -1)) == int(week)), None)
    if not module:
        raise ValueError("Module not found")
    concepts = ", ".join(module.get("concepts", []))
    prompt = f"""Write a 5-question multiple-choice quiz to check understanding of this learning module.

Module: {module.get('title')}
Objective: {module.get('objective')}
Concepts: {concepts}

Each question: a clear stem, exactly 4 options, the index (0-3) of the correct answer, and a one-line explanation.
Make them test real understanding, not trivia. Return STRICT JSON."""
    quiz = claude_json(prompt=prompt, model=CLAUDE_FLASH, max_tokens=2000, temperature=0.5, schema=QUIZ_SCHEMA)
    return quiz or {"questions": []}
