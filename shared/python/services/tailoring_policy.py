"""Versioned, dependency-free policy for evidence-led tailoring."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from contextvars import ContextVar
from functools import wraps

VERSION = "evidence-v1"
CONTENT_KEYS = ("contact", "summary", "experience", "education", "skills", "projects", "certifications", "skills_section_title")
_active = ContextVar("evidence_tailoring_policy", default=False)


def enabled() -> bool:
    # Explicit opt-in in every environment; never change a running run's policy.
    return _active.get() or os.environ.get("EVIDENCE_TAILORING_ENABLED", "false").lower() in {"true", "1", "on"}


def policy_context(fn):
    """A versioned run keeps its policy even if rollout is disabled later."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = _active.set(True)
        try:
            return fn(*args, **kwargs)
        finally:
            _active.reset(token)
    return wrapped


def content(resume: dict) -> dict:
    return {k: copy.deepcopy(v) for k, v in resume.items() if k in CONTENT_KEYS}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def content_hash(resume: dict) -> str:
    return digest(content(resume))


WRITING_RULES = """Write a clear, natural resume for this job using the selected source facts.
Make supported capabilities visible even when the source uses different vocabulary.
Retries can demonstrate failure recovery; indexed query improvements can demonstrate
performance optimization. Related tools are not interchangeable. A title alone does
not establish typical duties. Keep actual employers, titles, dates, ownership, tools,
metrics, comparators, approximation and outcomes. Do not invent causal relationships.
Reframe substantially where useful; keep strong original wording where already clear.
One coherent accomplishment per bullet. Several tools are fine when they explain the
work, but never append unrelated keywords. No keyword percentage or repetition quota.
Do not copy job-description sentences or make every prior role sound like this job.
Keep specific work objects and contribution; avoid abstract capability strings,
inflated verbs, filler, forced metrics, and mandatory STAR/XYZ sentence templates.
Plain qualitative accomplishments are valid. Natural repeated verbs are acceptable.
Use concise sentences and readable length, not a fixed character or technology quota.
Preserve the user's requested exclusions and wording locks. Never refill whitespace
with invented projects or accomplishments. Skills listed without work context may
stay in Skills, but cannot establish use at an employer. Return only requested patches.
"""

REVIEW_RULES = """Assess ONLY the visible resume against the job description. Do not assume
facts not on the page. Identify the apparent role/level, specific reasons to interview,
and unresolved concerns. Check both hidden relevant strengths and over-tailoring:
keyword lists, repeated claims, inflated phrasing, abstract labels replacing actual
work, and implausibly identical roles. Accurate professional terminology is welcome;
overlap with the JD is not itself a defect. Prefer clarity with equivalent supported
coverage. Do not produce vendor scores, screening probabilities, or culture-fit guesses.
Every finding must cite an exact passage from the visible resume. Unknown is unknown.
"""
