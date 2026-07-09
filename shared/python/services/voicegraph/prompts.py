"""Shuffleable writing prompts (§6.12.2).

These are never mandatory. They are designed to elicit *facts and voice
simultaneously* — the answers feed both the knowledge graph (what is true) and the
style profile (how the person talks). Co-owned by prompt-scientist + recruiter-panel.
"""
from typing import Any, Dict, List

WRITING_PROMPTS: List[Dict[str, str]] = [
    {
        "id": "built_or_fixed",
        "text": "Tell me about something you built or fixed that you're proud of. What broke, what you tried first, and what actually worked?",
    },
    {
        "id": "explain_outsider",
        "text": "Explain what you do to someone outside your field.",
    },
    {
        "id": "decision_defend",
        "text": "Describe a decision you made that you would still defend.",
    },
    {
        "id": "rare_skill",
        "text": "What is something you know how to do that most people in your role do not?",
    },
    {
        "id": "last_month",
        "text": "Walk me through your last month at work.",
    },
    {
        "id": "free_form",
        "text": "Or just write about anything.",
    },
]

_IDS = {p["id"] for p in WRITING_PROMPTS}


def list_prompts() -> List[Dict[str, str]]:
    return list(WRITING_PROMPTS)


def is_valid_prompt(prompt_id: Any) -> bool:
    return prompt_id in _IDS
