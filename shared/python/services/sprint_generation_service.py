"""Learning Hub — AI generation (runs inside the async `sprint_generate` job).

Transforms the universal objective spine into 14 realistic, company-style
engineering tickets tailored to a canonical role + difficulty. Generated in two
batches (days 1-7, 8-14) so rich output never truncates, then merged and written
to the SHARED `sprint_paths` doc keyed by path_key. Ticket numbers are assigned
deterministically in code (stable format) so the model focuses on content.

Because the doc is shared per (role, difficulty, version, season), this runs
~roles×tiers times product-wide, then is reused until CURRICULUM_VERSION bumps.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List

from utils.db_connect import DBConnect
from services.claude_client import claude_json, CLAUDE_PRO
from services import career_paths
from services import sprint_curriculum as curriculum
from services import sprint_challenge_service as challenge_service

logger = logging.getLogger(__name__)

# Project's Python schema DSL (converted to a Claude tool input_schema by
# _json_repair.python_schema_to_json_schema) — NOT JSON Schema. Note the DSL has
# no `bool` branch: a bool sentinel silently degrades to a string field, so any
# yes/no field must be a str with an explicit enum stated in the prompt.
TICKETS_SCHEMA = {
    "project_name": str,
    "tickets": [{
        "day": int,
        "title": str,
        "business_context": str,
        "sprint_objective": str,
        "priority": str,
        "estimated_time": str,
        "acceptance_criteria": [str],
        "deliverables": [str],
        "helpful_resources": [{"label": str, "url": str}],
    }],
}


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _generate_batch(role_label: str, difficulty: str, project_name: str,
                    spine_slice: List[Dict]) -> List[Dict]:
    diff_label = career_paths.DIFFICULTY_LABELS.get(difficulty, difficulty)
    day_lines = "\n".join(
        f"- Day {sd['day']} (key: {sd['key']}): {sd['objective']}" for sd in spine_slice
    )
    prompt = f"""You are an engineering manager writing Jira tickets for a real \
sprint at a fast-moving tech company. Turn each day's objective into an \
immersive, realistic engineering ticket for a {diff_label} {role_label}.

The product/project is called "{project_name}". Keep every ticket consistent \
with that same product so the days feel like one continuous body of work that \
builds on itself.

Days to write (preserve the exact `day` number for each):
{day_lines}

For EACH day produce a ticket object with:
- day: the exact day number given above
- title: a punchy feature/task name (NOT the generic objective) — e.g. "Payment Authentication Service"
- business_context: 1-2 sentences on WHY this matters to the business/users
- sprint_objective: one crisp sentence on what to build/do this sprint
- priority: one of "Low", "Medium", "High", "Critical"
- estimated_time: realistic for a {diff_label} engineer (e.g. "3-4 hours")
- acceptance_criteria: 3-5 concrete criteria, EACH verifiable by reading the \
finished public GitHub repo (a file exists and contains X, a service/target/config \
is defined, docs cover Y). Never write a criterion that can only be confirmed by \
running commands or by another person's judgement — no "exits with code 0", no \
"a teammate confirms", no "the endpoint returns 200". State the artifact instead.
- deliverables: 2-4 tangible artifacts (code, config, docs, endpoints, dashboards...)
- helpful_resources: 2-3 items, each a real documentation URL OR a Google search URL (https://www.google.com/search?q=...) with a short label

Make it feel like actual work an engineer performs inside a company — never a \
lesson or tutorial. Calibrate scope + wording to a {diff_label} engineer. \
Return STRICT JSON matching the schema."""
    result = claude_json(prompt=prompt, model=CLAUDE_PRO, max_tokens=8000, temperature=0.5, schema=TICKETS_SCHEMA)
    return (result or {}).get("tickets") or []


def generate_path(role_key: str, difficulty: str, season_id: str) -> List[Dict]:
    """Assemble the `days[]` structure.

    V1 (incremental rollout): only the PUBLISHED days (see
    `sprint_curriculum.PUBLISHED_THROUGH_DAY`) get real AI-generated tickets;
    every other day is a `coming_soon` placeholder with `ticket=None`. When more
    days are published later this generates them automatically — no code change.
    """
    meta = career_paths.path_meta(role_key)
    role_label = meta["label"]
    spine = curriculum.spine_for_season(season_id)
    project_name = f"{role_label} Platform"

    published = [sd for sd in spine if curriculum.is_published(sd["day"])]

    raw: Dict[int, Dict] = {}
    # Generate published days in batches of 7 (only Day 1 today, so one call).
    for i in range(0, len(published), 7):
        for t in _generate_batch(role_label, difficulty, project_name, published[i:i + 7]):
            try:
                raw[int(t.get("day"))] = t
            except (TypeError, ValueError):
                continue

    days: List[Dict] = []
    for sd in spine:
        if not curriculum.is_published(sd["day"]):
            # Placeholder — no AI content generated for unreleased days.
            days.append({"day": sd["day"], "key": sd["key"], "theme": sd["theme"],
                         "ticket": None, "published": False})
            continue
        t = raw.get(sd["day"], {})
        ticket = {
            "ticket_number": f"{meta['code']}-{200 + sd['day']}",
            "sprint_label": f"Sprint {sd['day']}",
            "title": (t.get("title") or sd["objective"]).strip(),
            "business_context": (t.get("business_context") or "").strip(),
            "sprint_objective": (t.get("sprint_objective") or sd["objective"]).strip(),
            "priority": _norm_priority(t.get("priority")),
            "difficulty": career_paths.DIFFICULTY_LABELS.get(difficulty, difficulty),
            "estimated_time": (t.get("estimated_time") or "3-4 hours").strip(),
            "acceptance_criteria": [str(a).strip() for a in (t.get("acceptance_criteria") or []) if str(a).strip()],
            "deliverables": [str(d).strip() for d in (t.get("deliverables") or []) if str(d).strip()],
            "helpful_resources": _norm_resources(t.get("helpful_resources")),
            "challenge_type": sd["key"],
        }
        days.append({"day": sd["day"], "key": sd["key"], "theme": sd["theme"],
                     "ticket": ticket, "published": True})
    return days


def _norm_priority(p) -> str:
    valid = {"low": "Low", "medium": "Medium", "high": "High", "critical": "Critical"}
    return valid.get(str(p or "").strip().lower(), "Medium")


def _norm_resources(items) -> List[Dict]:
    out = []
    for it in (items or []):
        if isinstance(it, dict):
            label = str(it.get("label") or "Resource").strip()
            url = str(it.get("url") or "").strip()
        else:
            label, url = str(it).strip(), ""
        if not url:
            url = f"https://www.google.com/search?q={label.replace(' ', '+')}"
        out.append({"label": label, "url": url})
    return out[:4]


def generate_path_job(payload: Dict) -> Dict:
    """Entry point invoked from resume_service._process_job (job_type='sprint_generate').

    Writes the shared sprint_paths doc, then returns the requesting user's full
    stateful view so the poll resolves directly to a renderable path.
    """
    user_email = payload.get("user_email", "")
    path_key = payload["path_key"]
    role_key = payload["canonical_role"]
    difficulty = payload["difficulty"]
    season_id = payload.get("season_id", curriculum.DEFAULT_SEASON)
    curriculum_version = payload.get("curriculum_version", curriculum.CURRICULUM_VERSION)
    is_preview = bool(payload.get("is_preview"))

    # Idempotency: if another invocation already finished, just return the view.
    existing = _db().sprint_paths.find_one({"path_key": path_key})
    if existing and existing.get("status") == "ready":
        return _view_or_status(user_email, payload)

    days = generate_path(role_key, difficulty, season_id)
    if not days or not any((d.get("ticket") or {}).get("acceptance_criteria") for d in days):
        _db().sprint_paths.update_one(
            {"path_key": path_key},
            {"$set": {"status": "failed", "updated_at": _now()}},
            upsert=True,
        )
        raise ValueError("Sprint path generation returned no usable tickets")

    meta = career_paths.path_meta(role_key)
    _db().sprint_paths.update_one(
        {"path_key": path_key},
        {"$set": {
            "path_key": path_key,
            "canonical_role": role_key,
            "role_label": meta["label"],
            "role_code": meta["code"],
            "difficulty": difficulty,
            "curriculum_version": curriculum_version,
            "season_id": season_id,
            "is_preview": is_preview,
            "status": "ready",
            "days": days,
            "updated_at": _now(),
        },
         "$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )
    return _view_or_status(user_email, payload)


def _view_or_status(user_email: str, payload: Dict) -> Dict:
    ctx = {
        "role_key": payload["canonical_role"],
        "role_label": career_paths.path_meta(payload["canonical_role"])["label"],
        "role_code": career_paths.path_meta(payload["canonical_role"])["code"],
        "difficulty": payload["difficulty"],
        "season_id": payload.get("season_id", curriculum.DEFAULT_SEASON),
        "curriculum_version": payload.get("curriculum_version", curriculum.CURRICULUM_VERSION),
        "path_key": payload["path_key"],
        "is_preview": bool(payload.get("is_preview")),
    }
    view = challenge_service.build_path_view(user_email, ctx)
    return view or {"status": "failed", "path_key": payload["path_key"]}
