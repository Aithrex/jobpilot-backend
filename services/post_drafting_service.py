"""AI career-narrative post drafting (Pillar C).

Turns an attended/won event — or any achievement — into audience-tailored
LinkedIn posts. Generates 3 distinct angles so the user picks a voice, and
stays HONEST: a 'won' claim must be backed by a recorded relation (proof); when
specifics aren't provided we hedge rather than fabricate.

Runs as job_type 'post_draft' on the async substrate (PRO).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from utils.db_connect import DBConnect
from services.gemini_client import gemini_json, GEMINI_PRO

logger = logging.getLogger(__name__)

AUDIENCES = {
    "engineering": "fellow engineers (technical depth, what you built, the hard problem)",
    "product": "product/PM peers (the problem, the user impact, the metric)",
    "design": "designers (the craft, the UX thinking, the before/after)",
    "general": "a broad professional network (clear, human, achievement-forward)",
}

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "angle": {"type": "string"},
                    "hook": {"type": "string"},
                    "text": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def draft_post(payload: Dict) -> Dict:
    """Job entry (job_type='post_draft'). Returns {draft_id, variants}."""
    user_email = payload.get("user_email", "")
    audience = (payload.get("audience") or "general").lower()
    if audience not in AUDIENCES:
        audience = "general"
    relation = (payload.get("relation") or "attended").lower()
    achievement = (payload.get("achievement") or "").strip()

    # Event context (optional).
    event_ctx = ""
    won_proven = False
    event_id = payload.get("event_id")
    if event_id:
        ev = _db().events.find_one({"event_id": event_id}) or {}
        ue = _db().user_events.find_one({"user_email": user_email, "event_id": event_id}) or {}
        won_proven = ue.get("relation") == "won"
        event_ctx = (
            f"Event: {ev.get('title')} ({ev.get('type')}) by {ev.get('org')}. "
            f"Tags: {', '.join(ev.get('tags') or [])}. Prize: {ev.get('prize_pool')}. "
            f"User's relation: {ue.get('relation') or relation}. Outcome notes: {ue.get('outcome') or ''}."
        )

    # Light resume context for grounding (optional).
    resume_ctx = ""
    try:
        base = _db().user_resumes.find_one({"user_email": user_email, "type": "base"}) or {}
        parsed = base.get("parsed") or {}
        if parsed.get("summary"):
            resume_ctx = f"Candidate summary: {parsed.get('summary')[:300]}"
    except Exception:
        pass

    integrity = (
        "INTEGRITY RULES: Do not invent facts, metrics, or placements. "
        + ("The user has a RECORDED win for this event, so you may state they won. "
           if (relation == "won" and won_proven) else
           "If the user did not provide a concrete result, do NOT claim a win or fabricate metrics — "
           "frame it around what they learned/built/contributed. ")
        + "Where a specific number would strengthen the post but wasn't provided, leave a clearly-marked "
        "[add your metric] placeholder instead of inventing one."
    )

    prompt = (
        f"You are a sharp personal-branding writer. Draft 3 DISTINCT LinkedIn post variants for {AUDIENCES[audience]}.\n"
        "Each variant: a different angle, a scroll-stopping first line (hook), an authentic body (<=120 words, "
        "line breaks ok, 1 light emoji max), and 3-5 relevant hashtags. Vary structure across the three "
        "(e.g. story, lessons-learned list, bold-claim+proof).\n\n"
        f"{event_ctx}\n{('Achievement: ' + achievement) if achievement else ''}\n{resume_ctx}\n\n"
        f"{integrity}\n\n"
        'Return strict JSON: {"variants": [{"angle": "...", "hook": "...", "text": "...", "hashtags": ["..."]}]}'
    )
    res = gemini_json(prompt, max_tokens=2500, temperature=0.7, model=GEMINI_PRO, schema=POST_SCHEMA) or {}
    variants = res.get("variants") or []

    draft_id = str(uuid.uuid4())
    _db().post_drafts.insert_one({
        "draft_id": draft_id,
        "user_email": user_email,
        "event_id": event_id,
        "audience": audience,
        "relation": relation,
        "variants": variants,
        "status": "draft",
        "created_at": _now(),
    })
    return {"draft_id": draft_id, "variants": variants}


def list_drafts(user_email: str, limit: int = 30):
    docs = list(_db().post_drafts.find({"user_email": user_email}).sort("created_at", -1).limit(limit))
    out = []
    for d in docs:
        d.pop("_id", None)
        ca = d.get("created_at")
        d["created_at"] = ca.isoformat() if hasattr(ca, "isoformat") else ca
        out.append(d)
    return out
