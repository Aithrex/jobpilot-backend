"""
Career Copilot — ReAct-style multi-agent orchestrator with tool-calling loop,
outreach campaign engine, adaptive dashboard, enhanced learning playground,
and memory/context management.

Architecture:
  - Orchestrator: sends user message + tool declarations to Claude (PRO tier);
    if the model returns tool calls, executes them locally, feeds results back,
    and loops (max 5 rounds) until the model returns a final text response.
  - Each tool is tagged with a specialist agent name for pipeline visualization.
  - State: `career_copilot_state` collection (per user_email).
  - Outreach: `career_copilot_outreach` collection (per user_email).
"""

from __future__ import annotations

import json
import re
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 1400
_CHUNK_OVERLAP = 200
_MAX_CHUNKS = 4
_HISTORY_CAP = 24
_MAX_ORCHESTRATOR_ROUNDS = 3
_ORCHESTRATOR_DEADLINE_SECONDS = 18.0

# ---------------------------------------------------------------------------
# Playground tracks (8 total: 3 original + 5 new)
# ---------------------------------------------------------------------------

PLAYGROUND_TRACKS: List[Dict[str, Any]] = [
    {
        "id": "cold_outreach",
        "title": "Cold outreach that gets replies",
        "description": "From research to follow-ups across email and LinkedIn (ethical, non-spammy).",
        "steps": [
            {
                "title": "Map the funnel",
                "type": "theory",
                "xp": 10,
                "body": "Define your ICP (role, company size, stage), your proof (1\u20132 assets), and a single CTA. "
                "Outreach is a system: research \u2192 note \u2192 connect \u2192 add value \u2192 follow up.",
            },
            {
                "title": "Draft 3 message variants",
                "type": "practice",
                "xp": 25,
                "body": "Write: (1) 80-word cold email, (2) LinkedIn connect note \u2264300 chars, (3) follow-up #48h. "
                "Use specifics from the company's recent post or product page \u2014 no generic praise.",
            },
            {
                "title": "Personalization hooks",
                "type": "theory",
                "xp": 10,
                "body": "Good hooks: shared stack, a blog post, a launch, a mutual problem. "
                "Bad hooks: 'I am passionate about' with no evidence.",
            },
            {
                "title": "A/B your subject lines",
                "type": "practice",
                "xp": 20,
                "body": "List 5 subject lines. Score each for curiosity vs clarity. "
                "Pick two and role-play the recipient \u2014 which would you open?",
            },
            {
                "title": "Cadence & limits",
                "type": "theory",
                "xp": 10,
                "body": "Respect platform ToS, avoid bulk automation, and cap follow-ups. "
                "Build a 14-day touch pattern you can sustain.",
            },
        ],
    },
    {
        "id": "system_design_interview",
        "title": "System design interview (practical path)",
        "description": "Progressive drills from requirements to tradeoffs, tailored to your background.",
        "steps": [
            {
                "title": "Clarify requirements",
                "type": "theory",
                "xp": 10,
                "body": "Functional (features), non-functional (scale, latency, consistency), and constraints. "
                "List 5 questions you'd ask before drawing a box.",
            },
            {
                "title": "5-minute high-level",
                "type": "practice",
                "xp": 20,
                "body": "Pick a system you know (e.g. URL shortener, chat). Sketch API + data model in bullets only \u2014 no diagram yet.",
            },
            {
                "title": "Capacity + bottlenecks",
                "type": "theory",
                "xp": 10,
                "body": "Back-of-napkin QPS, storage, hot keys, fan-out. Name the first bottleneck you'd measure in prod.",
            },
            {
                "title": "Deep dive one component",
                "type": "practice",
                "xp": 25,
                "body": "Take one component (e.g. cache, DB, queue) and list failure modes + mitigations.",
            },
            {
                "title": "Tradeoff close",
                "type": "theory",
                "xp": 10,
                "body": "Practice saying: 'We choose X over Y because \u2026 at our scale \u2026 accepting downside Z.'",
            },
        ],
    },
    {
        "id": "portfolio_project",
        "title": "Ship a portfolio project end-to-end",
        "description": "Idea to README to demo, with milestones you can show in an interview.",
        "steps": [
            {
                "title": "Problem + user",
                "type": "theory",
                "xp": 10,
                "body": "One sentence problem, one primary user, one 'why now'. If you can't name the user, narrow the scope.",
            },
            {
                "title": "One-page spec",
                "type": "practice",
                "xp": 25,
                "body": "Outcomes, non-goals, 3 user stories, stack proposal, 1-week slice for MVP.",
            },
            {
                "title": "Milestone plan",
                "type": "theory",
                "xp": 10,
                "body": "Break into demoable chunks: data \u2192 API \u2192 UI \u2192 deploy \u2192 story in README.",
            },
            {
                "title": "Build the smallest slice",
                "type": "practice",
                "xp": 25,
                "body": "Ship one vertical slice (e.g. read path + one mutation). No extra features until this works in prod or CI.",
            },
            {
                "title": "Tell the story",
                "type": "theory",
                "xp": 15,
                "body": "Write README: what/why, architecture diagram, how to run, what you'd do next. "
                "This is what recruiters skim.",
            },
        ],
    },
    {
        "id": "star_mastery",
        "title": "Behavioral interview STAR mastery",
        "description": "Master the Situation-Task-Action-Result framework for behavioral rounds.",
        "steps": [
            {
                "title": "Anatomy of STAR",
                "type": "theory",
                "xp": 10,
                "body": "Situation sets context (1\u20132 sentences), Task states YOUR responsibility, "
                "Action details the specific steps YOU took, Result quantifies impact. "
                "Anti-pattern: vague 'we' statements with no metrics.",
            },
            {
                "title": "Build your story bank",
                "type": "practice",
                "xp": 25,
                "body": "From your resume, pick 5 accomplishments. For each, write a STAR skeleton: "
                "S (1 sentence), T (1 sentence), A (3 bullets), R (1 quantified outcome).",
            },
            {
                "title": "Map stories to themes",
                "type": "theory",
                "xp": 10,
                "body": "Tag each story: leadership, conflict, failure, ambiguity, impact. "
                "Ensure you cover at least 4 of these \u2014 interviewers rotate through them.",
            },
            {
                "title": "Timed delivery drill",
                "type": "practice",
                "xp": 25,
                "body": "Pick one story. Set a 90-second timer and deliver it aloud or in writing. "
                "Cut filler, sharpen the Action section, and end with a number.",
            },
            {
                "title": "Handling curveballs",
                "type": "practice",
                "xp": 20,
                "body": "Practice: 'Tell me about a time you failed.' Reframe failure as learning + corrective action. "
                "Write a STAR answer where the Result includes what changed afterward.",
            },
        ],
    },
    {
        "id": "technical_patterns",
        "title": "Technical interview patterns",
        "description": "Pattern-based approach to coding interviews, from brute force to optimal.",
        "steps": [
            {
                "title": "The pattern toolkit",
                "type": "theory",
                "xp": 10,
                "body": "Core patterns: sliding window, two pointers, BFS/DFS, dynamic programming, "
                "binary search on answer, monotonic stack. Know when each applies.",
            },
            {
                "title": "Brute force first",
                "type": "practice",
                "xp": 20,
                "body": "Pick a medium-difficulty problem. Write the brute force solution first. "
                "State the time/space complexity. Interviewers want to see you can start somewhere.",
            },
            {
                "title": "Optimize with patterns",
                "type": "practice",
                "xp": 25,
                "body": "Take the same problem. Identify which pattern applies and re-solve. "
                "Explain why this pattern reduces complexity \u2014 what redundant work are you eliminating?",
            },
            {
                "title": "Edge cases & testing",
                "type": "theory",
                "xp": 10,
                "body": "Always test: empty input, single element, duplicates, negative values, overflow. "
                "Walk through your code with 2\u20133 test cases before saying 'done'.",
            },
            {
                "title": "Communication framework",
                "type": "theory",
                "xp": 10,
                "body": "Structure: restate the problem \u2192 ask clarifying questions \u2192 propose approach \u2192 code \u2192 test. "
                "Think aloud. Silence is your enemy in a technical interview.",
            },
            {
                "title": "Mock under pressure",
                "type": "practice",
                "xp": 25,
                "body": "Set a 25-minute timer. Solve a new problem end-to-end with commentary. "
                "Record yourself or write a transcript. Review for gaps.",
            },
        ],
    },
    {
        "id": "linkedin_brand",
        "title": "Personal brand on LinkedIn",
        "description": "Build a compelling LinkedIn presence that attracts opportunities.",
        "steps": [
            {
                "title": "Headline & about",
                "type": "practice",
                "xp": 20,
                "body": "Rewrite your headline: [Role] | [Key skill] | [Proof point or mission]. "
                "About section: 3 paragraphs \u2014 what you do, what you've built, what you're looking for.",
            },
            {
                "title": "Experience as storytelling",
                "type": "theory",
                "xp": 10,
                "body": "Each role should read like a mini case study: context \u2192 what you did \u2192 measurable result. "
                "Use bullet points with numbers. Avoid job-description copy-paste.",
            },
            {
                "title": "Content strategy",
                "type": "theory",
                "xp": 10,
                "body": "Post 2\u20133x/week: 1 insight from work, 1 industry take, 1 personal-professional story. "
                "Engagement beats broadcasting \u2014 comment thoughtfully on 5 posts daily.",
            },
            {
                "title": "Write your first post",
                "type": "practice",
                "xp": 25,
                "body": "Write a 150-word LinkedIn post about something you learned recently at work or in a project. "
                "Hook in the first line, value in the middle, question at the end.",
            },
            {
                "title": "Network with intent",
                "type": "practice",
                "xp": 20,
                "body": "Identify 10 people at target companies. Send personalized connect notes (\u2264300 chars) "
                "referencing their work. Track who accepts and follow up with value.",
            },
        ],
    },
    {
        "id": "salary_negotiation",
        "title": "Salary negotiation playbook",
        "description": "Research, frame, and close compensation conversations with confidence.",
        "steps": [
            {
                "title": "Market research",
                "type": "theory",
                "xp": 10,
                "body": "Use Levels.fyi, Glassdoor, Blind, and your network to find the band for your role + level + location. "
                "Know the 25th, 50th, and 75th percentiles.",
            },
            {
                "title": "Know your BATNA",
                "type": "theory",
                "xp": 10,
                "body": "Best Alternative to Negotiated Agreement: what's your walk-away option? "
                "A competing offer, current job, or freelance income all strengthen your position.",
            },
            {
                "title": "Frame your ask",
                "type": "practice",
                "xp": 25,
                "body": "Write a script: 'Based on [research] and [my experience in X], I'm targeting [range]. "
                "I'm excited about this role because [specific reason].' Practice saying it aloud.",
            },
            {
                "title": "Beyond base salary",
                "type": "theory",
                "xp": 10,
                "body": "Negotiate the full package: signing bonus, equity/RSU schedule, remote flexibility, "
                "title, review timeline, learning budget. Each has different cost to the employer.",
            },
            {
                "title": "Closing the conversation",
                "type": "practice",
                "xp": 20,
                "body": "Role-play the final exchange: 'If you can do [X], I'm ready to sign today.' "
                "Practice handling 'That's the best we can do' \u2014 silence, then ask about timeline for revisit.",
            },
        ],
    },
    {
        "id": "resume_impact",
        "title": "Resume impact metrics",
        "description": "Transform vague bullet points into quantified impact statements.",
        "steps": [
            {
                "title": "The XYZ formula",
                "type": "theory",
                "xp": 10,
                "body": "Accomplished [X] as measured by [Y] by doing [Z]. "
                "Every bullet needs a verb, a metric, and the method. 'Responsible for' is never a bullet start.",
            },
            {
                "title": "Mine your metrics",
                "type": "practice",
                "xp": 25,
                "body": "For each role, list: revenue impact, cost saved, time reduced, users affected, "
                "uptime improved, bugs fixed, tests added. If you don't have exact numbers, estimate with ranges.",
            },
            {
                "title": "Tailor to the JD",
                "type": "practice",
                "xp": 25,
                "body": "Take a real job description. Highlight the top 5 keywords. "
                "Rewrite 3 of your bullets to naturally incorporate those keywords while keeping honesty.",
            },
            {
                "title": "Peer review",
                "type": "practice",
                "xp": 20,
                "body": "Share your top 5 bullets with a friend or paste them here. "
                "Ask: 'Can you tell what I did, how well I did it, and why it mattered?' Revise based on gaps.",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_state_collection():
    from utils.db_connect import DBConnect
    return DBConnect().get_db().career_copilot_state


def _get_outreach_collection():
    from utils.db_connect import DBConnect
    return DBConnect().get_db().career_copilot_outreach


# ---------------------------------------------------------------------------
# RAG: chunking, tokenization, ranking (preserved from original)
# ---------------------------------------------------------------------------

def _tokenize(s: str) -> set:
    return set(re.findall(r"[a-z0-9#+]{2,}", (s or "").lower()))


def _chunk_text(text: str) -> List[Dict[str, Any]]:
    t = (text or "").strip()
    if not t:
        return []
    chunks: List[Dict[str, Any]] = []
    i = 0
    idx = 0
    n = len(t)
    while i < n:
        j = min(i + _CHUNK_SIZE, n)
        piece = t[i:j].strip()
        if piece:
            chunks.append({"id": f"c{idx}", "text": piece})
            idx += 1
        if j >= n:
            break
        i = j - _CHUNK_OVERLAP
    return chunks


def _rank_chunks(user_message: str, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    q = _tokenize(user_message)
    if not q:
        return chunks[:_MAX_CHUNKS]
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for c in chunks:
        ct = _tokenize(c["text"])
        inter = len(q & ct)
        union = len(q | ct) or 1
        score = inter / union + min(len(c["text"]), 5000) * 1e-9
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:_MAX_CHUNKS]]


def get_resume_rag_context(user_email: str, user_message: str) -> Tuple[str, List[Dict[str, str]]]:
    """Return formatted context + citation snippets from lexical RAG over stored resume text."""
    from services.resume_service import get_resume_service

    svc = get_resume_service()
    doc = None
    try:
        doc = svc.parser.ensure_structured_resume(user_email)
    except Exception as e:
        logger.warning("ensure_structured_resume failed: %s", e)

    raw = (doc or {}).get("raw_text") or ""
    if not raw and (doc or {}).get("structured"):
        try:
            raw = json.dumps((doc or {}).get("structured"), default=str)[:8000]
        except Exception:
            raw = str((doc or {}).get("structured"))[:8000]

    chunks = _chunk_text(raw)
    picked = _rank_chunks(user_message, chunks) if chunks else []
    if not picked:
        return ("", [])

    lines: List[str] = []
    cites: List[Dict[str, str]] = []
    for c in picked:
        snip = c["text"][:500].replace("\n", " ")
        if len(c["text"]) > 500:
            snip += "\u2026"
        lines.append(f"[{c['id']}] {c['text']}")
        cites.append({"id": c["id"], "snippet": snip})

    return ("\n\n---\n\n".join(lines), cites)


def _get_full_resume_text(user_email: str) -> str:
    """Get the full resume text for a user (used by tools that need complete context)."""
    from services.resume_service import get_resume_service
    svc = get_resume_service()
    try:
        doc = svc.parser.ensure_structured_resume(user_email)
    except Exception:
        return ""
    raw = (doc or {}).get("raw_text") or ""
    if not raw and (doc or {}).get("structured"):
        try:
            raw = json.dumps((doc or {}).get("structured"), default=str)[:12000]
        except Exception:
            raw = str((doc or {}).get("structured"))[:12000]
    return raw


# ---------------------------------------------------------------------------
# TOOL DEFINITIONS — callable functions for the ReAct orchestrator
# ---------------------------------------------------------------------------

def _tool_search_resume(user_email: str, query: str, **_) -> Dict[str, Any]:
    """RAG search over resume chunks."""
    ctx, cites = get_resume_rag_context(user_email, query)
    return {
        "context": ctx[:6000] if ctx else "No resume on file.",
        "citations": cites,
    }


def _tool_analyze_jd(jd_text: str, **_) -> Dict[str, Any]:
    """Extract required skills, company, role title, keywords from a JD."""
    from services.gemini_client import gemini_json, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        "Analyze this job description and extract structured information.\n\n"
        f"JOB DESCRIPTION:\n{(jd_text or '')[:12000]}\n\n"
        "Return JSON with: company, role_title, seniority_level, "
        "required_skills (list), preferred_skills (list), keywords (list of ATS-relevant terms), "
        "responsibilities_summary (3 bullets), culture_signals (list)."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=3000, temperature=0.2,
            schema={
                "company": str, "role_title": str, "seniority_level": str,
                "required_skills": [str], "preferred_skills": [str], "keywords": [str],
                "responsibilities_summary": [str], "culture_signals": [str],
            },
        )
    except Exception as e:
        logger.warning("analyze_jd failed: %s", e)
        return {"error": f"JD analysis failed: {str(e)[:200]}"}


def _tool_tailor_bullets(resume_section: str, jd_keywords: str, **_) -> Dict[str, Any]:
    """Rewrite resume bullets with impact metrics aligned to JD keywords."""
    from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        "Rewrite these resume bullet points to better align with the target role keywords "
        "while preserving honesty and adding quantified impact metrics where possible.\n\n"
        f"ORIGINAL BULLETS:\n{(resume_section or '')[:4000]}\n\n"
        f"TARGET KEYWORDS:\n{(jd_keywords or '')[:2000]}\n\n"
        "Return JSON with: tailored_bullets (list of objects with original, rewritten, changes_made), "
        "keyword_coverage (list of keywords addressed), improvement_summary."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_PRO, max_tokens=4000, temperature=0.3,
            schema={
                "tailored_bullets": [{"original": str, "rewritten": str, "changes_made": str}],
                "keyword_coverage": [str],
                "improvement_summary": str,
            },
        )
    except (ValueError, LLMRetriesExhaustedError):
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=4000, temperature=0.35,
            schema={
                "tailored_bullets": [{"original": str, "rewritten": str, "changes_made": str}],
                "keyword_coverage": [str],
                "improvement_summary": str,
            },
        )


def _tool_draft_cold_email(recipient_context: str, tone: str = "professional", word_limit: str = "120", **_) -> Dict[str, Any]:
    """Generate a cold outreach email."""
    from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        f"Draft a cold outreach email.\n\n"
        f"RECIPIENT CONTEXT: {(recipient_context or '')[:2000]}\n"
        f"TONE: {tone}\nWORD LIMIT: {word_limit}\n\n"
        "Rules: no fabricated connections, honest subject line, specific hook from recipient's work, "
        "clear CTA, respect their time.\n\n"
        "Return JSON: subject, body, cta, personalization_notes, word_count."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_PRO, max_tokens=2000, temperature=0.4,
            schema={"subject": str, "body": str, "cta": str, "personalization_notes": str, "word_count": int},
        )
    except (ValueError, LLMRetriesExhaustedError):
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=2000, temperature=0.45,
            schema={"subject": str, "body": str, "cta": str, "personalization_notes": str, "word_count": int},
        )


def _tool_draft_linkedin_message(recipient_context: str, char_limit: str = "300", **_) -> Dict[str, Any]:
    """Generate a LinkedIn connection note."""
    from services.gemini_client import gemini_json, GEMINI_FLASH

    prompt = (
        f"Write a LinkedIn connection note.\n\n"
        f"RECIPIENT: {(recipient_context or '')[:1500]}\n"
        f"CHARACTER LIMIT: {char_limit}\n\n"
        "Rules: personal, specific, no 'I'd love to pick your brain', reference their work.\n\n"
        "Return JSON: message, char_count, hook_used."
    )
    return gemini_json(
        prompt=prompt, model=GEMINI_FLASH, max_tokens=1000, temperature=0.4,
        schema={"message": str, "char_count": int, "hook_used": str},
    )


def _tool_draft_followup(original_message: str, days_since: str = "3", tone: str = "friendly", **_) -> Dict[str, Any]:
    """Generate a follow-up message."""
    from services.gemini_client import gemini_json, GEMINI_FLASH

    prompt = (
        f"Write a follow-up to this message sent {days_since} days ago.\n\n"
        f"ORIGINAL: {(original_message or '')[:2000]}\nTONE: {tone}\n\n"
        "Rules: add new value (article, insight, compliment on their work), "
        "don't guilt-trip, keep shorter than original.\n\n"
        "Return JSON: subject (if email), body, value_add, word_count."
    )
    return gemini_json(
        prompt=prompt, model=GEMINI_FLASH, max_tokens=1500, temperature=0.4,
        schema={"subject": str, "body": str, "value_add": str, "word_count": int},
    )


def _tool_generate_interview_plan(role: str, company: str, focus_areas: str = "", **_) -> Dict[str, Any]:
    """Generate a structured interview preparation plan."""
    from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        f"Create a structured interview prep plan.\n\n"
        f"ROLE: {role}\nCOMPANY: {company}\nFOCUS AREAS: {focus_areas or 'general'}\n\n"
        "Return JSON: role, company, prep_timeline_days (int), sections (list of "
        "{section_name, priority (high/medium/low), tasks (list of {task, time_estimate, resources})}), "
        "day_of_tips (list of strings)."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_PRO, max_tokens=4000, temperature=0.3,
            schema={
                "role": str, "company": str, "prep_timeline_days": int,
                "sections": [{"section_name": str, "priority": str, "tasks": [{"task": str, "time_estimate": str, "resources": str}]}],
                "day_of_tips": [str],
            },
        )
    except (ValueError, LLMRetriesExhaustedError):
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=4000, temperature=0.35,
            schema={
                "role": str, "company": str, "prep_timeline_days": int,
                "sections": [{"section_name": str, "priority": str, "tasks": [{"task": str, "time_estimate": str, "resources": str}]}],
                "day_of_tips": [str],
            },
        )


def _tool_generate_star_answer(question: str, resume_context: str = "", **_) -> Dict[str, Any]:
    """Generate a STAR-format answer grounded in the user's resume."""
    from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        f"Generate a STAR-format behavioral interview answer.\n\n"
        f"QUESTION: {question}\n"
        f"RESUME CONTEXT (ground the answer in this):\n{(resume_context or 'Not provided')[:4000]}\n\n"
        "Return JSON: question, situation, task, action (list of bullet strings), result, "
        "delivery_time_seconds (int), coaching_tip."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_PRO, max_tokens=3000, temperature=0.35,
            schema={
                "question": str, "situation": str, "task": str,
                "action": [str], "result": str,
                "delivery_time_seconds": int, "coaching_tip": str,
            },
        )
    except (ValueError, LLMRetriesExhaustedError):
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=3000, temperature=0.4,
            schema={
                "question": str, "situation": str, "task": str,
                "action": [str], "result": str,
                "delivery_time_seconds": int, "coaching_tip": str,
            },
        )


def _tool_scaffold_project(idea: str, stack: str = "", timeline: str = "2 weeks", **_) -> Dict[str, Any]:
    """Generate a project spec with milestones."""
    from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        f"Create a project scaffold/spec.\n\n"
        f"IDEA: {idea}\nSTACK: {stack or 'suggest appropriate'}\nTIMELINE: {timeline}\n\n"
        "Return JSON: name, description, tech_stack (list), "
        "milestones (list of {title, deliverable, duration}), "
        "folder_structure (list of path strings), readme_outline (list of section headings)."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_PRO, max_tokens=4000, temperature=0.35,
            schema={
                "name": str, "description": str, "tech_stack": [str],
                "milestones": [{"title": str, "deliverable": str, "duration": str}],
                "folder_structure": [str], "readme_outline": [str],
            },
        )
    except (ValueError, LLMRetriesExhaustedError):
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=4000, temperature=0.4,
            schema={
                "name": str, "description": str, "tech_stack": [str],
                "milestones": [{"title": str, "deliverable": str, "duration": str}],
                "folder_structure": [str], "readme_outline": [str],
            },
        )


def _tool_generate_ppt_outline(topic: str, audience: str = "general", slide_count: str = "10", **_) -> Dict[str, Any]:
    """Generate a slide-by-slide presentation outline with speaker notes."""
    from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        f"Create a presentation outline.\n\n"
        f"TOPIC: {topic}\nAUDIENCE: {audience}\nSLIDE COUNT: {slide_count}\n\n"
        "Return JSON: title, subtitle, "
        "slides (list of {title, bullets (list of strings), speaker_notes, layout_hint}), "
        "theme_suggestion."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_PRO, max_tokens=5000, temperature=0.35,
            schema={
                "title": str, "subtitle": str,
                "slides": [{"title": str, "bullets": [str], "speaker_notes": str, "layout_hint": str}],
                "theme_suggestion": str,
            },
        )
    except (ValueError, LLMRetriesExhaustedError):
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=5000, temperature=0.4,
            schema={
                "title": str, "subtitle": str,
                "slides": [{"title": str, "bullets": [str], "speaker_notes": str, "layout_hint": str}],
                "theme_suggestion": str,
            },
        )


def _tool_generate_pdf_onepager(content_type: str, context: str = "", **_) -> Dict[str, Any]:
    """Generate structured one-pager content (executive summary, project brief, etc)."""
    from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        f"Create a one-pager document.\n\n"
        f"TYPE: {content_type}\nCONTEXT:\n{(context or '')[:4000]}\n\n"
        "Return JSON: title, sections (list of {heading, body}), footer_note."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_PRO, max_tokens=4000, temperature=0.3,
            schema={
                "title": str,
                "sections": [{"heading": str, "body": str}],
                "footer_note": str,
            },
        )
    except (ValueError, LLMRetriesExhaustedError):
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=4000, temperature=0.35,
            schema={
                "title": str,
                "sections": [{"heading": str, "body": str}],
                "footer_note": str,
            },
        )


def _tool_quiz_me(topic: str, difficulty: str = "medium", count: str = "5", **_) -> Dict[str, Any]:
    """Generate quiz questions with answers."""
    from services.gemini_client import gemini_json, GEMINI_FLASH

    prompt = (
        f"Generate quiz questions.\n\n"
        f"TOPIC: {topic}\nDIFFICULTY: {difficulty}\nCOUNT: {count}\n\n"
        "Return JSON: topic, difficulty, "
        "questions (list of {question, options (list of 4 strings), correct_index (int 0-3), explanation})."
    )
    return gemini_json(
        prompt=prompt, model=GEMINI_FLASH, max_tokens=4000, temperature=0.4,
        schema={
            "topic": str, "difficulty": str,
            "questions": [{"question": str, "options": [str], "correct_index": int, "explanation": str}],
        },
    )


def _tool_evaluate_answer(question: str, user_answer: str, rubric: str = "", **_) -> Dict[str, Any]:
    """Score a user's answer and provide feedback."""
    from services.gemini_client import gemini_json, GEMINI_FLASH

    prompt = (
        f"Evaluate this answer.\n\n"
        f"QUESTION: {question}\n"
        f"USER'S ANSWER:\n{(user_answer or '')[:3000]}\n"
        f"RUBRIC: {rubric or 'accuracy, completeness, clarity'}\n\n"
        "Return JSON: score (int 1-10), strengths (list), improvements (list), "
        "model_answer, overall_feedback."
    )
    return gemini_json(
        prompt=prompt, model=GEMINI_FLASH, max_tokens=2000, temperature=0.3,
        schema={
            "score": int, "strengths": [str], "improvements": [str],
            "model_answer": str, "overall_feedback": str,
        },
    )


# ---------------------------------------------------------------------------
# Tool registry — maps tool names to (function, agent_tag, description, params)
# ---------------------------------------------------------------------------

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "search_resume": {
        "fn": _tool_search_resume,
        "agent": "memory",
        "needs_email": True,
        "description": "Search the user's resume using RAG to find relevant experience, skills, or achievements.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to find in the resume"},
            },
            "required": ["query"],
        },
    },
    "analyze_jd": {
        "fn": _tool_analyze_jd,
        "agent": "tailor",
        "needs_email": False,
        "description": "Analyze a job description to extract required skills, company info, role title, and ATS keywords.",
        "parameters": {
            "type": "object",
            "properties": {
                "jd_text": {"type": "string", "description": "The job description text to analyze"},
            },
            "required": ["jd_text"],
        },
    },
    "tailor_bullets": {
        "fn": _tool_tailor_bullets,
        "agent": "tailor",
        "needs_email": False,
        "description": "Rewrite resume bullet points to align with target JD keywords and add impact metrics.",
        "parameters": {
            "type": "object",
            "properties": {
                "resume_section": {"type": "string", "description": "Resume bullet points to tailor"},
                "jd_keywords": {"type": "string", "description": "Target keywords from the job description"},
            },
            "required": ["resume_section", "jd_keywords"],
        },
    },
    "draft_cold_email": {
        "fn": _tool_draft_cold_email,
        "agent": "outreach",
        "needs_email": False,
        "description": "Draft a cold outreach email with a personalized hook and clear CTA.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_context": {"type": "string", "description": "Info about the recipient (name, role, company, recent work)"},
                "tone": {"type": "string", "description": "Tone of the email (professional, casual, enthusiastic)"},
                "word_limit": {"type": "string", "description": "Target word count"},
            },
            "required": ["recipient_context"],
        },
    },
    "draft_linkedin_message": {
        "fn": _tool_draft_linkedin_message,
        "agent": "outreach",
        "needs_email": False,
        "description": "Draft a LinkedIn connection note that's personal and specific.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_context": {"type": "string", "description": "Info about the recipient"},
                "char_limit": {"type": "string", "description": "Character limit (default 300)"},
            },
            "required": ["recipient_context"],
        },
    },
    "draft_followup": {
        "fn": _tool_draft_followup,
        "agent": "outreach",
        "needs_email": False,
        "description": "Draft a follow-up message that adds new value.",
        "parameters": {
            "type": "object",
            "properties": {
                "original_message": {"type": "string", "description": "The original message that was sent"},
                "days_since": {"type": "string", "description": "Days since the original message"},
                "tone": {"type": "string", "description": "Desired tone"},
            },
            "required": ["original_message"],
        },
    },
    "generate_interview_plan": {
        "fn": _tool_generate_interview_plan,
        "agent": "interview",
        "needs_email": False,
        "description": "Generate a structured interview preparation plan with timeline and focus areas.",
        "parameters": {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Target role"},
                "company": {"type": "string", "description": "Target company"},
                "focus_areas": {"type": "string", "description": "Specific areas to focus on"},
            },
            "required": ["role", "company"],
        },
    },
    "generate_star_answer": {
        "fn": _tool_generate_star_answer,
        "agent": "interview",
        "needs_email": False,
        "description": "Generate a STAR-format behavioral interview answer grounded in the user's experience.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The behavioral interview question"},
                "resume_context": {"type": "string", "description": "Relevant resume context to ground the answer in"},
            },
            "required": ["question"],
        },
    },
    "scaffold_project": {
        "fn": _tool_scaffold_project,
        "agent": "project",
        "needs_email": False,
        "description": "Generate a project specification with milestones, tech stack, and folder structure.",
        "parameters": {
            "type": "object",
            "properties": {
                "idea": {"type": "string", "description": "Project idea description"},
                "stack": {"type": "string", "description": "Preferred tech stack"},
                "timeline": {"type": "string", "description": "Target timeline"},
            },
            "required": ["idea"],
        },
    },
    "generate_ppt_outline": {
        "fn": _tool_generate_ppt_outline,
        "agent": "project",
        "needs_email": False,
        "description": "Generate a slide-by-slide presentation outline with speaker notes.",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Presentation topic"},
                "audience": {"type": "string", "description": "Target audience"},
                "slide_count": {"type": "string", "description": "Number of slides"},
            },
            "required": ["topic"],
        },
    },
    "generate_pdf_onepager": {
        "fn": _tool_generate_pdf_onepager,
        "agent": "project",
        "needs_email": False,
        "description": "Generate structured one-pager content (executive summary, project brief, etc).",
        "parameters": {
            "type": "object",
            "properties": {
                "content_type": {"type": "string", "description": "Type of one-pager (executive_summary, project_brief, case_study, etc)"},
                "context": {"type": "string", "description": "Context and details for the one-pager"},
            },
            "required": ["content_type"],
        },
    },
    "quiz_me": {
        "fn": _tool_quiz_me,
        "agent": "interview",
        "needs_email": False,
        "description": "Generate quiz questions with multiple-choice answers on a topic.",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Quiz topic"},
                "difficulty": {"type": "string", "description": "easy, medium, or hard"},
                "count": {"type": "string", "description": "Number of questions"},
            },
            "required": ["topic"],
        },
    },
    "evaluate_answer": {
        "fn": _tool_evaluate_answer,
        "agent": "interview",
        "needs_email": False,
        "description": "Evaluate a user's answer to a question and provide scoring and feedback.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question that was asked"},
                "user_answer": {"type": "string", "description": "The user's answer to evaluate"},
                "rubric": {"type": "string", "description": "Evaluation criteria"},
            },
            "required": ["question", "user_answer"],
        },
    },
}


# ---------------------------------------------------------------------------
# Function-calling declarations (built from registry)
# Format follows the legacy Gemini function-declaration shape; the provider
# abstraction translates these to Claude/Bedrock tool-use blocks at runtime.
# ---------------------------------------------------------------------------

def _build_function_declarations() -> List[Dict[str, Any]]:
    """Build function declarations from the tool registry.

    Schema mirrors the legacy Gemini function-calling format; the provider
    abstraction translates these to Claude/Bedrock tool-use blocks.
    """
    decls = []
    for name, spec in TOOL_REGISTRY.items():
        decls.append({
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        })
    return decls


# ---------------------------------------------------------------------------
# Orchestrator system prompt
# ---------------------------------------------------------------------------

COPILOT_SYSTEM = """You are the lead orchestrator for a multi-agent career copilot. You have specialist tools at your disposal — use them when the user's request would benefit from structured data retrieval, generation, or analysis.

Specialist agents behind the tools:
- Memory & RAG (search_resume): searches the user's own resume text chunks
- Career strategist: job search, positioning, negotiation framing
- Resume & JD tailor (analyze_jd, tailor_bullets): line-level tailoring, keywords, impact metrics
- Interview coach (generate_interview_plan, generate_star_answer, quiz_me, evaluate_answer): behavioral/technical plans, STAR, drills
- Outreach & GTM (draft_cold_email, draft_linkedin_message, draft_followup): cold email, LinkedIn, follow-ups — always ethical
- Project & deliverables (scaffold_project, generate_ppt_outline, generate_pdf_onepager): scoping, milestones, presentations, one-pagers

Rules:
1) Ground every claim about the USER in their resume data. If data is missing, say so and ask for it.
2) Use tools when the request involves structured generation (emails, plans, analyses, quizzes). For simple conversational replies, respond directly.
3) Be concise by default; expand on request. Use markdown formatting.
4) For outreach: always ethical — no spam, no fabricated connections, honest subject lines, respect platform ToS.
5) For deliverables (PPT, PDF, project): return structured JSON that the frontend can render richly.
6) Match the user's language if clearly not English; otherwise English."""


# ---------------------------------------------------------------------------
# ReAct orchestrator loop
# ---------------------------------------------------------------------------

def _execute_tool(tool_name: str, args: Dict[str, Any], user_email: str) -> Tuple[Dict[str, Any], str]:
    """Execute a tool from the registry. Returns (result, agent_tag)."""
    spec = TOOL_REGISTRY.get(tool_name)
    if not spec:
        return {"error": f"Unknown tool: {tool_name}"}, "system"

    fn = spec["fn"]
    agent_tag = spec["agent"]

    if spec.get("needs_email"):
        args["user_email"] = user_email

    try:
        result = fn(**args)
        return result, agent_tag
    except Exception as e:
        logger.exception("Tool %s failed: %s", tool_name, e)
        return {"error": f"Tool {tool_name} failed: {str(e)[:300]}"}, agent_tag


def _run_orchestrator(
    user_email: str,
    message: str,
    resume_ctx: str,
    history_text: str,
    jd_block: str,
    memory_notes: List[Dict[str, str]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """ReAct-style tool-calling loop.

    Delegates to the active LLM provider's `tool_call()` implementation so
    the orchestrator works on both Gemini (function calling) and Claude
    (tool use on Bedrock).

    Returns (final_reply_text, pipeline_log).
    """
    from services.gemini_client import get_active_provider

    provider = get_active_provider()

    memory_block = ""
    if memory_notes:
        lines = [f"- {n['key']}: {n['value']}" for n in memory_notes]
        memory_block = "\nUSER_PINNED_CONTEXT:\n" + "\n".join(lines) + "\n"

    system_text = (
        f"{COPILOT_SYSTEM}\n\n"
        f"RESUME_CONTEXT (RAG):\n"
        f"{resume_ctx or 'NO RESUME TEXT ON FILE — ask user to upload in Resume > My Resumes or paste summary.'}\n"
        f"{jd_block}"
        f"{memory_block}\n"
        f"RECENT_TRANSCRIPT:\n{history_text or '(empty)'}\n"
    )

    function_declarations = _build_function_declarations()

    # Bind user_email into the tool executor so the provider's tool loop
    # can call (tool_name, args) without knowing about session identity.
    def _exec(tool_name: str, args: Dict[str, Any]):
        return _execute_tool(tool_name, args, user_email)

    return provider.tool_call(
        system_text=system_text,
        user_message=message,
        tools=function_declarations,
        execute_tool=_exec,
        max_rounds=_MAX_ORCHESTRATOR_ROUNDS,
        deadline_seconds=_ORCHESTRATOR_DEADLINE_SECONDS,
        temperature=0.35,
        max_tokens=2800,
    )


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def get_copilot_state(user_email: str) -> Dict[str, Any]:
    col = _get_state_collection()
    doc = col.find_one({"user_email": user_email})
    if doc:
        doc.pop("_id", None)
    return doc or {}


def get_messages_for_api(user_email: str) -> List[Dict[str, Any]]:
    st = get_copilot_state(user_email)
    return _serialize_messages(list(st.get("messages") or []))


def upsert_copilot_state(user_email: str, patch: Dict[str, Any]) -> None:
    col = _get_state_collection()
    patch["user_email"] = user_email
    patch["updated_at"] = datetime.now(timezone.utc)
    col.update_one(
        {"user_email": user_email},
        {"$set": patch, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def _serialize_messages(msgs: List[Dict]) -> List[Dict]:
    out: List[Dict] = []
    for m in msgs:
        o = {**m}
        at = o.get("at")
        if hasattr(at, "isoformat"):
            o["at"] = at.isoformat()
        if "meta" in o and o["meta"] is not None and not isinstance(o["meta"], dict):
            o["meta"] = str(o["meta"])
        out.append(o)
    return out


def _trim_history(msgs: List[Dict]) -> List[Dict]:
    return msgs[-_HISTORY_CAP:]


# ---------------------------------------------------------------------------
# Behavior tracking
# ---------------------------------------------------------------------------

def _touch_behavior(user_email: str, source_tab: str) -> None:
    col = _get_state_collection()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    col.update_one(
        {"user_email": user_email},
        {
            "$inc": {f"behavior.visits.{source_tab}": 1},
            "$set": {
                "behavior.last_tab": source_tab,
                "updated_at": datetime.now(timezone.utc),
            },
            "$addToSet": {"behavior.active_days": today},
            "$setOnInsert": {
                "user_email": user_email,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )


def _bump_topic(user_email: str, topic: str) -> None:
    col = _get_state_collection()
    col.update_one(
        {"user_email": user_email},
        {
            "$inc": {f"behavior.topics.{topic}": 1},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )


def record_tab_event(user_email: str, tab: str) -> None:
    """Cross-tab behavior for adaptive dashboard."""
    if not tab:
        return
    _touch_behavior(user_email, tab)


def _record_activity_event(user_email: str, event_type: str, description: str) -> None:
    """Append an event to the activity timeline."""
    col = _get_state_collection()
    event = {
        "type": event_type,
        "description": description,
        "timestamp": datetime.now(timezone.utc),
    }
    col.update_one(
        {"user_email": user_email},
        {
            "$push": {
                "activity_timeline": {
                    "$each": [event],
                    "$slice": -50,
                },
            },
            "$set": {"updated_at": datetime.now(timezone.utc)},
            "$setOnInsert": {
                "user_email": user_email,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Memory & context management
# ---------------------------------------------------------------------------

def save_memory_note(user_email: str, key: str, value: str) -> Dict[str, Any]:
    """Pin a context note (e.g., 'remote only', 'target: PM roles')."""
    if not key or not value:
        return {"ok": False, "error": "key and value are required"}
    col = _get_state_collection()
    col.update_one(
        {"user_email": user_email},
        {
            "$set": {
                f"memory_notes.{key}": value,
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "user_email": user_email,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )
    _record_activity_event(user_email, "memory", f"Pinned note: {key}")
    return {"ok": True, "key": key, "value": value}


def get_memory_notes(user_email: str) -> List[Dict[str, str]]:
    """Retrieve all pinned memory notes."""
    st = get_copilot_state(user_email)
    notes_dict = st.get("memory_notes") or {}
    return [{"key": k, "value": v} for k, v in notes_dict.items()]


def delete_memory_note(user_email: str, key: str) -> Dict[str, Any]:
    """Remove a pinned memory note."""
    if not key:
        return {"ok": False, "error": "key is required"}
    col = _get_state_collection()
    col.update_one(
        {"user_email": user_email},
        {
            "$unset": {f"memory_notes.{key}": ""},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )
    return {"ok": True, "deleted": key}


def get_session_timeline(user_email: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Chronological activity across all features."""
    st = get_copilot_state(user_email)
    timeline = st.get("activity_timeline") or []
    result = []
    for evt in timeline[-limit:]:
        e = {**evt}
        ts = e.get("timestamp")
        if hasattr(ts, "isoformat"):
            e["timestamp"] = ts.isoformat()
        result.append(e)
    return result


# ---------------------------------------------------------------------------
# Main orchestrator entry point
# ---------------------------------------------------------------------------

def run_career_copilot(
    user_email: str,
    message: str,
    jd_paste: str = "",
    reset: bool = False,
) -> Dict[str, Any]:
    col = _get_state_collection()
    if reset:
        col.update_one(
            {"user_email": user_email},
            {"$set": {
                "messages": [],
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        return {"ok": True, "cleared": True}

    st = get_copilot_state(user_email)
    history: List[Dict] = st.get("messages") or []

    text_l = message.lower()
    if any(k in text_l for k in ("linkedin", "dm", "cold email", "recruiter", "outreach")):
        _bump_topic(user_email, "outreach")
    if any(k in text_l for k in ("interview", "star", "behavioral", "system design")):
        _bump_topic(user_email, "interview")
    if any(k in text_l for k in ("resume", "bullet", "tailor", "ats", "jd", "job description")):
        _bump_topic(user_email, "tailor")
    if any(k in text_l for k in ("project", "scaffold", "portfolio", "build")):
        _bump_topic(user_email, "project")
    if any(k in text_l for k in ("ppt", "slide", "presentation", "pdf", "one-pager")):
        _bump_topic(user_email, "deliverable")

    resume_ctx, citations = get_resume_rag_context(user_email, message)

    convo = []
    for m in _trim_history(history):
        role = m.get("role")
        if role in ("user", "assistant") and m.get("content"):
            convo.append(f"{'User' if role == 'user' else 'Assistant'}: {m['content'][:4000]}")
    history_text = "\n".join(convo[-10:]) if convo else "(empty)"

    jd_block = ""
    if (jd_paste or "").strip():
        jd_block = f"\nPASTED_JOB_DESCRIPTION (truncated):\n{(jd_paste or '')[:12000]}\n"

    memory_notes = get_memory_notes(user_email)

    reply, pipeline_log = _run_orchestrator(
        user_email=user_email,
        message=message,
        resume_ctx=resume_ctx,
        history_text=history_text,
        jd_block=jd_block,
        memory_notes=memory_notes,
    )

    if not pipeline_log:
        pipeline_log = [
            {"agent": "strategist", "label": "analyze", "summary": "Analyzed user request and formulated response"},
        ]

    now = datetime.now(timezone.utc)
    new_history = _trim_history(
        history
        + [
            {"role": "user", "content": message, "at": now, "id": uuid.uuid4().hex[:8]},
            {
                "role": "assistant",
                "content": reply,
                "at": now,
                "id": uuid.uuid4().hex[:8],
                "meta": {
                    "pipeline": pipeline_log,
                    "citations": citations,
                },
            },
        ],
    )

    suggested = _generate_suggested_prompts(message, reply)

    col.update_one(
        {"user_email": user_email},
        {
            "$set": {
                "messages": new_history,
                "last_reply_meta": {
                    "suggested_prompts": suggested,
                    "pipeline": pipeline_log,
                },
                "updated_at": now,
            },
            "$setOnInsert": {
                "user_email": user_email,
                "created_at": now,
            },
        },
        upsert=True,
    )
    _touch_behavior(user_email, "copilot")
    _record_activity_event(user_email, "copilot", f"Message: {message[:80]}")

    return {
        "ok": True,
        "reply": reply,
        "pipeline": pipeline_log,
        "citations": citations,
        "rag_grounding": f"Used {len(citations)} resume chunks" if citations else "No resume on file",
        "suggested_prompts": suggested,
        "next_best_actions": _compute_next_best_actions(user_email),
        "compliance": "All outreach suggestions follow ethical guidelines: no spam, honest subject lines, platform ToS respected.",
        "messages": _serialize_messages(new_history),
    }


def _generate_suggested_prompts(user_message: str, reply: str) -> List[str]:
    """Generate 3 follow-up prompts based on the conversation context (fast, no model call)."""
    text_l = user_message.lower()

    if any(k in text_l for k in ("email", "outreach", "cold")):
        return [
            "Now write a LinkedIn connect note for the same person",
            "Draft a follow-up if they don't reply in 3 days",
            "Help me build an outreach campaign for this company",
        ]
    if any(k in text_l for k in ("interview", "star", "behavioral")):
        return [
            "Give me 3 more STAR stories from my resume",
            "Create a full interview prep plan for this role",
            "Quiz me on behavioral interview questions",
        ]
    if any(k in text_l for k in ("resume", "tailor", "bullet", "jd")):
        return [
            "Analyze the JD and list missing keywords",
            "Rewrite my experience bullets for this role",
            "Score my resume against this job description",
        ]
    if any(k in text_l for k in ("project", "scaffold", "build")):
        return [
            "Create a presentation outline for this project",
            "Generate a one-pager executive summary",
            "Break this into weekly milestones",
        ]
    return [
        "Tailor my resume to a JD (I'll paste it next)",
        "Draft a cold email to a hiring manager at ___",
        "Create an interview prep plan for ___",
    ]


def _compute_next_best_actions(user_email: str) -> List[Dict[str, Any]]:
    """Compute prioritized next-best-actions based on behavior patterns."""
    st = get_copilot_state(user_email)
    behavior = st.get("behavior") or {}
    topics = behavior.get("topics") or {}
    visits = behavior.get("visits") or {}
    messages = st.get("messages") or []
    pg = st.get("playground") or {}
    notes = st.get("memory_notes") or {}

    from services.resume_service import get_resume_service
    has_resume = False
    try:
        doc = get_resume_service().parser.ensure_structured_resume(user_email)
        has_resume = bool((doc or {}).get("raw_text") or (doc or {}).get("structured"))
    except Exception:
        pass

    actions = []

    if not has_resume:
        actions.append({
            "title": "Upload your base resume",
            "reason": "RAG and tailoring work best with your full text on file.",
            "priority": 100,
        })

    if len(messages) < 3:
        actions.append({
            "title": "Paste a job description",
            "reason": "I can align bullets, stories, and outreach to a specific role.",
            "priority": 90,
        })

    if not notes:
        actions.append({
            "title": "Pin your preferences",
            "reason": "Tell me things like 'remote only' or 'targeting PM roles' so I always remember.",
            "priority": 70,
        })

    outreach_count = topics.get("outreach", 0)
    interview_count = topics.get("interview", 0)
    tailor_count = topics.get("tailor", 0)

    if outreach_count > interview_count and outreach_count > tailor_count:
        actions.append({
            "title": "Start an outreach campaign",
            "reason": "You've been focused on messaging — let me help you sequence a full campaign.",
            "priority": 80,
        })
    elif interview_count > outreach_count:
        actions.append({
            "title": "Run a mock interview drill",
            "reason": "Practice makes perfect — try a timed STAR answer.",
            "priority": 80,
        })
    else:
        actions.append({
            "title": "Try the learning playground",
            "reason": "Structured tracks for outreach, interviews, projects, and more.",
            "priority": 60,
        })

    if pg.get("active_track") and not pg.get("finished_at"):
        actions.append({
            "title": f"Continue: {pg.get('custom_title') or pg.get('active_track')}",
            "reason": f"You're on step {(pg.get('current_step') or 0) + 1} of {len(pg.get('steps') or [])}.",
            "priority": 85,
        })

    actions.sort(key=lambda a: -a.get("priority", 0))
    return [{"title": a["title"], "reason": a["reason"]} for a in actions[:4]]


# ---------------------------------------------------------------------------
# Enhanced adaptive dashboard
# ---------------------------------------------------------------------------

def get_dashboard_bundle(user_email: str) -> Dict[str, Any]:
    """State + tracks + KPIs + activity timeline + rule-based nudges."""
    st = get_copilot_state(user_email)
    behavior = st.get("behavior") or {}
    topics = behavior.get("topics") or {}
    visits = behavior.get("visits") or {}
    messages = st.get("messages") or []
    active_days = behavior.get("active_days") or []

    from services.resume_service import get_resume_service
    has_resume = False
    try:
        doc = get_resume_service().parser.ensure_structured_resume(user_email)
        has_resume = bool((doc or {}).get("raw_text") or (doc or {}).get("structured"))
    except Exception:
        pass

    msg_count = sum(1 for m in messages if m.get("role") == "user")
    outreach_col = _get_outreach_collection()
    outreach_count = outreach_col.count_documents({"user_email": user_email})

    pg = st.get("playground") or {}
    tracks_completed = len(
        [1 for h in (st.get("playground_history") or []) if h.get("finished_at")]
    )
    if pg.get("finished_at"):
        tracks_completed += 1

    days_active = len(active_days)
    streak = _compute_streak(active_days)

    kpi_cards = [
        {"label": "Messages sent", "value": msg_count, "trend": "up" if msg_count > 5 else "neutral", "icon": "message"},
        {"label": "Outreach campaigns", "value": outreach_count, "trend": "up" if outreach_count > 0 else "neutral", "icon": "send"},
        {"label": "Tracks completed", "value": tracks_completed, "trend": "up" if tracks_completed > 0 else "neutral", "icon": "check-circle"},
        {"label": "Days active", "value": days_active, "trend": "up" if days_active > 3 else "neutral", "icon": "calendar"},
        {"label": "Current streak", "value": streak, "trend": "up" if streak > 1 else "neutral", "icon": "flame"},
    ]

    timeline_raw = st.get("activity_timeline") or []
    activity_timeline = []
    for evt in timeline_raw[-10:]:
        e = {**evt}
        ts = e.get("timestamp")
        if hasattr(ts, "isoformat"):
            e["timestamp"] = ts.isoformat()
        activity_timeline.append(e)

    total_topic_count = sum(topics.values()) or 1
    focus_distribution = {t: round(c / total_topic_count * 100, 1) for t, c in topics.items()}

    sorted_topics = sorted(topics.items(), key=lambda x: -x[1])
    favorite_topics = [t for t, _ in sorted_topics[:3]]

    jds_seen = topics.get("tailor", 0)

    memory_context = {
        "resume_uploaded": has_resume,
        "jds_seen": jds_seen,
        "total_messages": msg_count,
        "favorite_topics": favorite_topics,
    }

    last_meta = st.get("last_reply_meta") or {}
    suggested_prompts = last_meta.get("suggested_prompts") or [
        "Tailor my resume to this JD (I'll paste next)",
        "Write a 120-word cold email to a hiring manager for ___",
        "What should I improve first for senior SWE interviews?",
    ]

    nba = _compute_next_best_actions(user_email)

    return {
        "has_resume": has_resume,
        "last_tab": behavior.get("last_tab"),
        "visits": visits,
        "topics": topics,
        "kpi_cards": kpi_cards,
        "activity_timeline": activity_timeline,
        "focus_distribution": focus_distribution,
        "memory_context": memory_context,
        "suggested_prompts": suggested_prompts,
        "next_best_actions": nba,
        "playground": pg,
        "tracks": PLAYGROUND_TRACKS,
    }


def _compute_streak(active_days: List[str]) -> int:
    """Compute consecutive days streak up to today."""
    if not active_days:
        return 0
    try:
        dates = sorted(set(datetime.strptime(d, "%Y-%m-%d").date() for d in active_days), reverse=True)
    except (ValueError, TypeError):
        return 0

    today = datetime.now(timezone.utc).date()
    if not dates or (dates[0] != today and dates[0] != today - timedelta(days=1)):
        return 0

    streak = 1
    for i in range(1, len(dates)):
        if dates[i - 1] - dates[i] == timedelta(days=1):
            streak += 1
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# Enhanced learning playground
# ---------------------------------------------------------------------------

def start_playground_track(user_email: str, track_id: Optional[str], custom_topic: Optional[str]) -> Dict[str, Any]:
    if not (custom_topic and custom_topic.strip()) and not (track_id and str(track_id).strip()):
        return {"ok": False, "error": "Provide track_id or custom_topic"}

    if custom_topic and custom_topic.strip():
        from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError
        topic = custom_topic.strip()[:500]
        prompt = (
            f"You design a progressive learning + practice path (basic to advanced) for: {topic}\n"
            "Return JSON with: title (str), steps: array of 5-7 items, each: "
            "title, type (theory or practice), xp (int, 10 for theory, 20-25 for practice), "
            "body (2-4 sentences, actionable). No markdown in strings."
        )
        try:
            plan = gemini_json(
                prompt=prompt, model=GEMINI_PRO, max_tokens=4096, temperature=0.4,
                schema={
                    "title": str,
                    "steps": [{"title": str, "type": str, "xp": int, "body": str}],
                },
            )
        except (ValueError, LLMRetriesExhaustedError):
            plan = gemini_json(
                prompt=prompt, model=GEMINI_FLASH, max_tokens=4096, temperature=0.4,
                schema={
                    "title": str,
                    "steps": [{"title": str, "type": str, "xp": int, "body": str}],
                },
            )
        custom_steps = (plan or {}).get("steps") or []
        bundle = {
            "active_track": "custom",
            "custom_title": (plan or {}).get("title") or "Custom track",
            "custom_topic": topic,
            "current_step": 0,
            "steps": custom_steps,
            "completed": [],
            "xp_earned": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        t = next((x for x in PLAYGROUND_TRACKS if x["id"] == track_id), None)
        if not t:
            return {"ok": False, "error": "Unknown track"}
        bundle = {
            "active_track": track_id,
            "custom_title": None,
            "custom_topic": None,
            "current_step": 0,
            "steps": t["steps"],
            "completed": [],
            "xp_earned": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

    upsert_copilot_state(user_email, {"playground": bundle})
    _touch_behavior(user_email, "playground")
    _record_activity_event(
        user_email, "playground",
        f"Started track: {bundle.get('custom_title') or bundle.get('active_track')}",
    )
    return {"ok": True, "playground": bundle}


def complete_playground_step(user_email: str) -> Dict[str, Any]:
    st = get_copilot_state(user_email)
    pg = st.get("playground") or {}
    if not pg.get("steps"):
        return {"ok": False, "error": "No active track"}

    idx = int(pg.get("current_step") or 0)
    steps: List = pg.get("steps") or []
    done: List = list(pg.get("completed") or [])
    xp_earned = int(pg.get("xp_earned") or 0)

    if idx < len(steps):
        step = steps[idx]
        step_xp = int(step.get("xp") or (25 if step.get("type") == "practice" else 10))
        xp_earned += step_xp
        done.append(idx)
        idx += 1

    pg["completed"] = done
    pg["current_step"] = idx
    pg["xp_earned"] = xp_earned

    finished = idx >= len(steps)
    assessment = None

    if finished:
        pg["finished_at"] = datetime.now(timezone.utc).isoformat()
        _record_activity_event(
            user_email, "playground",
            f"Completed track: {pg.get('custom_title') or pg.get('active_track')} (+{xp_earned} XP)",
        )
        track_title = pg.get("custom_title") or pg.get("active_track") or "this topic"
        assessment = _generate_track_assessment(track_title)
    else:
        _record_activity_event(
            user_email, "playground",
            f"Completed step {idx} of {pg.get('custom_title') or pg.get('active_track')}",
        )

    _touch_behavior(user_email, "playground")

    col = _get_state_collection()
    update_ops: Dict[str, Any] = {"playground": pg}

    total_xp = int((st.get("xp") or {}).get("total", 0)) + (int(steps[idx - 1].get("xp") or 10) if idx > 0 and idx <= len(steps) else 0)
    col.update_one(
        {"user_email": user_email},
        {
            "$set": {
                "playground": pg,
                "xp.total": total_xp,
                "updated_at": datetime.now(timezone.utc),
            },
        },
    )

    result = {"ok": True, "playground": pg, "xp_earned": xp_earned, "total_xp": total_xp}
    if assessment:
        result["assessment"] = assessment
    return result


def _generate_track_assessment(track_title: str) -> Optional[Dict[str, Any]]:
    """Generate end-of-track assessment quiz (3-5 questions)."""
    from services.gemini_client import gemini_json, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        f"Generate 4 assessment questions for someone who just completed a learning track on: {track_title}\n\n"
        "Mix of multiple-choice and short-answer. Test practical application, not just recall.\n\n"
        "Return JSON: questions (list of {question, type ('mcq' or 'short_answer'), "
        "options (list of 4 strings for mcq, empty for short_answer), correct_index (int for mcq, -1 for short_answer), "
        "model_answer, explanation})."
    )
    try:
        return gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=3000, temperature=0.4,
            schema={
                "questions": [{
                    "question": str, "type": str, "options": [str],
                    "correct_index": int, "model_answer": str, "explanation": str,
                }],
            },
        )
    except Exception as e:
        logger.warning("Assessment generation failed: %s", e)
        return None


def generate_quiz(user_email: str, topic: str, difficulty: str = "medium", count: int = 4) -> Dict[str, Any]:
    """Public API: generate quiz questions on a topic with given difficulty."""
    from services.gemini_client import gemini_json, GEMINI_FLASH, LLMRetriesExhaustedError

    count = max(3, min(int(count), 5))
    prompt = (
        f"Generate {count} {difficulty}-difficulty quiz questions on: {topic}\n\n"
        "Each question must be multiple choice with 4 options, one correct.\n"
        "Test practical understanding and application, not just recall.\n\n"
        "Return JSON: questions (list of {question, options (list of 4 strings), "
        "correct_index (int 0-3), explanation (1-2 sentences)})."
    )
    try:
        result = gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=3000, temperature=0.4,
            schema={
                "questions": [{
                    "question": str, "options": [str],
                    "correct_index": int, "explanation": str,
                }],
            },
        )
    except Exception as e:
        logger.warning("Quiz generation failed: %s", e)
        return {"ok": False, "error": f"Quiz generation failed: {str(e)[:200]}"}

    qs = (result or {}).get("questions") or []
    _record_activity_event(user_email, "playground", f"Generated quiz: {topic} ({difficulty}, {len(qs)} Qs)")
    return {"ok": True, "questions": qs}


def submit_playground_answer(user_email: str, question: str, answer: str) -> Dict[str, Any]:
    """Evaluate a user's answer to a playground/assessment question."""
    if not question or not answer:
        return {"ok": False, "error": "question and answer are required"}

    result = _tool_evaluate_answer(question=question, user_answer=answer)
    _record_activity_event(
        user_email, "playground",
        f"Submitted answer (score: {result.get('score', '?')}/10)",
    )
    return {"ok": True, **result}


def reset_playground(user_email: str) -> None:
    st = get_copilot_state(user_email)
    pg = st.get("playground") or {}
    if pg.get("active_track") and pg.get("started_at"):
        col = _get_state_collection()
        col.update_one(
            {"user_email": user_email},
            {"$push": {"playground_history": pg}},
        )
    upsert_copilot_state(user_email, {"playground": {}})


# ---------------------------------------------------------------------------
# Outreach workflow engine
# ---------------------------------------------------------------------------

def create_outreach_campaign(
    user_email: str,
    target_company: str,
    target_role: str,
    contacts: List[Dict[str, str]],
    channel: str = "email",
) -> Dict[str, Any]:
    """Create a new outreach campaign with auto-generated sequence."""
    if not target_company or not target_role:
        return {"ok": False, "error": "target_company and target_role are required"}

    if channel not in ("email", "linkedin", "twitter"):
        channel = "email"

    resume_ctx = _get_full_resume_text(user_email)
    sequence = generate_outreach_sequence(target_company, target_role, channel, resume_ctx)

    campaign_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)

    sanitized_contacts = []
    for c in (contacts or []):
        sanitized_contacts.append({
            "name": str(c.get("name", ""))[:200],
            "title": str(c.get("title", ""))[:200],
            "notes": str(c.get("notes", ""))[:500],
        })

    campaign = {
        "campaign_id": campaign_id,
        "user_email": user_email,
        "target_company": target_company[:200],
        "target_role": target_role[:200],
        "channel": channel,
        "contacts": sanitized_contacts,
        "sequence": sequence,
        "created_at": now,
        "updated_at": now,
        "status": "active",
    }

    col = _get_outreach_collection()
    col.insert_one(campaign)

    _record_activity_event(
        user_email, "outreach",
        f"Created campaign: {target_role} at {target_company}",
    )

    campaign.pop("_id", None)
    return {"ok": True, "campaign": _serialize_campaign(campaign)}


def get_outreach_campaigns(user_email: str) -> List[Dict[str, Any]]:
    """List all campaigns for a user."""
    col = _get_outreach_collection()
    campaigns = list(col.find({"user_email": user_email}).sort("created_at", -1))
    return [_serialize_campaign(c) for c in campaigns]


def update_campaign_step(
    user_email: str,
    campaign_id: str,
    step_index: int,
    status: str,
    notes: str = "",
) -> Dict[str, Any]:
    """Update a specific step in a campaign sequence."""
    if status not in ("pending", "sent", "replied", "skipped"):
        return {"ok": False, "error": "Invalid status"}

    col = _get_outreach_collection()
    campaign = col.find_one({"campaign_id": campaign_id, "user_email": user_email})
    if not campaign:
        return {"ok": False, "error": "Campaign not found"}

    sequence = campaign.get("sequence") or []
    if step_index < 0 or step_index >= len(sequence):
        return {"ok": False, "error": "Invalid step index"}

    now = datetime.now(timezone.utc)
    update_fields = {
        f"sequence.{step_index}.status": status,
        f"sequence.{step_index}.notes": notes[:500],
        "updated_at": now,
    }
    if status == "sent":
        update_fields[f"sequence.{step_index}.sent_at"] = now

    all_done = all(
        s.get("status") in ("sent", "replied", "skipped")
        for i, s in enumerate(sequence) if i != step_index
    ) and status in ("sent", "replied", "skipped")

    if all_done:
        update_fields["status"] = "completed"

    col.update_one(
        {"campaign_id": campaign_id, "user_email": user_email},
        {"$set": update_fields},
    )

    _record_activity_event(
        user_email, "outreach",
        f"Updated step {step_index + 1}: {status} ({campaign.get('target_company')})",
    )

    updated = col.find_one({"campaign_id": campaign_id, "user_email": user_email})
    return {"ok": True, "campaign": _serialize_campaign(updated)}


def generate_outreach_sequence(
    company: str,
    role: str,
    channel: str,
    resume_context: str,
) -> List[Dict[str, Any]]:
    """Use the LLM to generate a 3-5 step outreach sequence."""
    from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH, LLMRetriesExhaustedError

    prompt = (
        f"Generate an outreach sequence for applying to {role} at {company} via {channel}.\n\n"
        f"RESUME CONTEXT:\n{(resume_context or 'Not available')[:4000]}\n\n"
        "Create a 4-step ethical outreach sequence: research, initial contact, value-add follow-up, closing follow-up.\n"
        "Each step: step (int 1-4), action (short label), template (the actual message/action text).\n"
        "Rules: no spam, honest, specific to company, respect platform ToS.\n\n"
        "Return JSON: steps (list of {step, action, template})."
    )
    try:
        result = gemini_json(
            prompt=prompt, model=GEMINI_PRO, max_tokens=4000, temperature=0.35,
            schema={"steps": [{"step": int, "action": str, "template": str}]},
        )
    except (ValueError, LLMRetriesExhaustedError):
        result = gemini_json(
            prompt=prompt, model=GEMINI_FLASH, max_tokens=4000, temperature=0.4,
            schema={"steps": [{"step": int, "action": str, "template": str}]},
        )

    raw_steps = (result or {}).get("steps") or []
    sequence = []
    for s in raw_steps:
        sequence.append({
            "step": s.get("step", len(sequence) + 1),
            "action": s.get("action", ""),
            "template": s.get("template", ""),
            "status": "pending",
            "sent_at": None,
            "notes": "",
        })
    return sequence


def _serialize_campaign(campaign: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a campaign document for API response."""
    c = {**campaign}
    c.pop("_id", None)
    for key in ("created_at", "updated_at"):
        if hasattr(c.get(key), "isoformat"):
            c[key] = c[key].isoformat()
    for step in c.get("sequence") or []:
        if hasattr(step.get("sent_at"), "isoformat"):
            step["sent_at"] = step["sent_at"].isoformat()
    return c
