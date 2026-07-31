"""Learning Hub — AI Mentor.

A senior-engineer persona that answers questions about the CURRENT ticket:
guidance, debugging direction, architecture reasoning, and hints — but never a
complete solution. Uses the FLASH tier (interactive, high-frequency chat), unlike
the one-time PRO path generation.

Runs synchronously (a single short turn); the blueprint gates spend
(`hub_mentor`) before calling and sanitizes the question.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from services.claude_client import claude_json, CLAUDE_FLASH

logger = logging.getLogger(__name__)

# Project's Python schema DSL, not JSON Schema — see sprint_generation_service.
ANSWER_SCHEMA = {
    "answer": str,
    "followups": [str],
}

_SYSTEM = (
    "You are a seasoned senior/staff software engineer mentoring a teammate who "
    "is working on a specific engineering ticket. Give sharp, practical guidance: "
    "debugging direction, architectural reasoning, trade-offs, and HINTS that lead "
    "them to the answer. NEVER paste a full working solution, a complete file, or "
    "the entire implementation — if they ask you to 'just give the code' or 'do it "
    "for me', coach them through it instead with a nudge and a next step. Be concise, "
    "encouraging, and concrete."
)


def ask(question: str, ticket: Optional[Dict] = None, role_label: str = "",
        difficulty: str = "", history: Optional[List[Dict]] = None) -> Dict:
    """Answer a mentor question with the current ticket as context."""
    ticket = ticket or {}
    ctx_lines = []
    if role_label:
        ctx_lines.append(f"Mentee role: {difficulty} {role_label}".strip())
    if ticket:
        ctx_lines.append(f"Ticket: {ticket.get('ticket_number', '')} — {ticket.get('title', '')}")
        if ticket.get("sprint_objective"):
            ctx_lines.append(f"Objective: {ticket['sprint_objective']}")
        ac = ticket.get("acceptance_criteria") or []
        if ac:
            ctx_lines.append("Acceptance criteria:\n" + "\n".join(f"  - {a}" for a in ac[:6]))
        deliverables = ticket.get("deliverables") or []
        if deliverables:
            ctx_lines.append("Deliverables: " + "; ".join(deliverables[:6]))
    context = "\n".join(ctx_lines) or "(no ticket context provided)"

    convo = ""
    for m in (history or [])[-6:]:
        role = "Mentee" if m.get("role") == "user" else "Mentor"
        convo += f"\n{role}: {m.get('content', '')}"

    prompt = f"""{_SYSTEM}

--- Current ticket context ---
{context}
--- Recent conversation ---{convo or ' (none)'}

Mentee's question: {question}

Respond as the mentor. Return STRICT JSON with:
- answer: your guidance (markdown ok, hints not full solutions)
- followups: 0-3 short suggested next questions the mentee might ask"""

    result = claude_json(prompt=prompt, model=CLAUDE_FLASH, max_tokens=1200, temperature=0.4, schema=ANSWER_SCHEMA)
    result = result or {}
    return {
        "answer": (result.get("answer") or "Let's break this down — what have you tried so far, and where exactly does it break?").strip(),
        "followups": [str(f).strip() for f in (result.get("followups") or []) if str(f).strip()][:3],
    }
