"""LinkedIn self-audit — honest profile coaching within real API limits.

LinkedIn's official API exposes NO profile data, and scraping violates their ToS —
so we NEVER pretend to read a user's live profile. Instead we use the data we
legitimately have — their parsed résumé + stated target role — to generate
specific, actionable guidance the user applies themselves, plus a ready-to-paste
headline and About section in their own voice. Helps a student present as a
legitimate, well-positioned candidate. Single FLASH call; never fabricates
experience.
"""
from __future__ import annotations

import logging
from typing import Dict

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)


def _db():
    return DBConnect().get_db()


def audit(user_email: str, linkedin_url: str = "") -> Dict:
    from services.resume_parser import ResumeParser
    from services.gemini_client import gemini_json, GEMINI_FLASH
    from schemas.resume_schemas import flatten_skills

    if linkedin_url:
        _db().user_preferences.update_one(
            {"user_email": user_email},
            {"$set": {"linkedin_url": linkedin_url}},
            upsert=True,
        )
    prefs = _db().user_preferences.find_one({"user_email": user_email}) or {}
    target_roles = prefs.get("target_roles") or []
    target_role = target_roles[0] if target_roles else "your target role"

    resume = ResumeParser().ensure_structured_resume(user_email=user_email)
    if not resume or not resume.get("structured"):
        raise ValueError("Upload a résumé first so we can tailor your LinkedIn audit.")
    s = resume["structured"]
    contact = s.get("contact") or {}
    name = contact.get("name") or "the candidate"
    summary = (s.get("summary") or "")[:500]
    exp = [f"{(e.get('title') or '').strip()} at {(e.get('company') or '').strip()}"
           for e in (s.get("experience") or [])[:3]]
    skills = (flatten_skills(s) or [])[:18]

    prompt = (
        "You are a LinkedIn optimization coach. Using ONLY the candidate's real "
        "résumé data below, produce specific guidance to make their LinkedIn profile "
        f"stand out for: {target_role}. NEVER invent experience, titles, or metrics. "
        "Write a strong headline and an 'About' section in their authentic voice, and "
        "audit the key profile sections with one concrete action each.\n\n"
        f"Name: {name}\nSummary: {summary}\n"
        f"Recent roles: {', '.join([e for e in exp if e.strip(' at')]) or 'n/a'}\n"
        f"Skills: {', '.join(map(str, skills)) or 'n/a'}\n\n"
        'Return strict JSON: {"score": 0-100, "headline": "<=120 chars", '
        '"about": "2-3 short paragraphs", "sections": [{"name": "Headline|About|'
        'Experience|Skills|Featured|...", "status": "good|improve|missing", '
        '"tip": "one specific action"}]}'
    )
    res = gemini_json(
        prompt, model=GEMINI_FLASH, temperature=0.4, max_tokens=1200,
        schema={"score": int, "headline": str, "about": str, "sections": list},
    )
    if not res:
        raise ValueError("Could not generate the audit — try again.")

    sections = []
    for sec in (res.get("sections") or [])[:8]:
        if not isinstance(sec, dict):
            continue
        status = str(sec.get("status") or "improve")
        if status not in ("good", "improve", "missing"):
            status = "improve"
        sections.append({
            "name": str(sec.get("name") or "")[:60],
            "status": status,
            "tip": str(sec.get("tip") or "")[:300],
        })

    return {
        "linkedin_url": linkedin_url or prefs.get("linkedin_url") or contact.get("linkedin") or "",
        "score": max(0, min(100, int(res.get("score") or 0))),
        "headline": str(res.get("headline") or "")[:200],
        "about": str(res.get("about") or "")[:1500],
        "sections": sections,
        "target_role": target_role,
    }
