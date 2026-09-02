"""Full-JD requirement analyzer (Phase 2 / Increment 5).

The legacy extractor sent only the first 5,000 characters to the LLM, so a
mandatory license in the last paragraph of a long posting never reached the
resume. This module always scans the COMPLETE JD:

  1. Deterministic requirement records with source spans (licenses, clearance,
     work authorization, education, years, location, travel).
  2. Classification of LLM-extracted skills as mandatory / preferred.
  3. A logistics/knockout split so sponsorship and clearance never become
     resume keywords.

``enrich(jd_analysis, jd_text)`` is additive: it keeps today's flat
``required_skills`` / ``preferred_skills`` lists and attaches ``requirements``.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from services.fit_score import (
    LICENSE_RULES,
    _CLEARANCE_JD,
    _WORKAUTH_JD,
)

# Work-authorization / sponsorship language must never be stuffed into the
# skills list. These are application questions, not resume keywords.
_LOGISTICS_RE = re.compile(
    r"(work authorization|authorized to work|visa|sponsorship|sponsor|"
    r"h-?1b|opt\b|cpt\b|green card|citizenship|citizen|e-?verify|"
    r"relocation|remote|hybrid|on-?site|onsite|travel|"
    r"background check|drug (test|screen))",
    re.IGNORECASE,
)

_KNOCKOUT_HINTS = re.compile(
    r"(must (have|hold|possess|be)|required|required:|minimum of|"
    r"without exception|will not be considered|do not apply)",
    re.IGNORECASE,
)

_PREFERRED_HINTS = re.compile(
    r"(preferred|nice to have|plus|bonus|ideally|a plus|desired)",
    re.IGNORECASE,
)

_YEARS_RE = re.compile(
    r"(\d+)\+?\s*(?:to|-|–)?\s*(\d+)?\s*\+?\s*years?",
    re.IGNORECASE,
)

_DEGREE_RE = re.compile(
    r"(bachelor|master|associate|phd|ph\.d|doctorate|mba|jd\b|md\b|"
    r"bsn|b\.s\.|m\.s\.|high school|ged)",
    re.IGNORECASE,
)


def _req_id(text: str, kind: str) -> str:
    raw = f"{kind}:{text.strip().lower()}"
    return "req_" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:10]


def _span(jd_text: str, needle: str) -> Dict[str, int]:
    if not needle:
        return {}
    idx = jd_text.lower().find(needle.lower())
    if idx < 0:
        return {}
    return {"start": idx, "end": idx + len(needle)}


def _sentence_at(jd_text: str, start: int) -> str:
    if start < 0:
        return ""
    left = max(0, jd_text.rfind(".", 0, start) + 1)
    right = jd_text.find(".", start)
    if right < 0:
        right = min(len(jd_text), start + 240)
    return jd_text[left:right].strip()


def _priority_for_sentence(sentence: str) -> str:
    if _PREFERRED_HINTS.search(sentence) and not _KNOCKOUT_HINTS.search(sentence):
        return "preferred"
    if _KNOCKOUT_HINTS.search(sentence):
        return "mandatory"
    return "contextual"


def _logistics_type(sentence: str) -> str:
    s = sentence.lower()
    if re.search(r"clearance|ts/sci|top secret|polygraph", s):
        return "clearance"
    if re.search(r"authoriz|visa|sponsor|citizen|h-?1b|opt\b", s):
        return "work_auth"
    if re.search(r"travel", s):
        return "travel"
    if re.search(r"remote|hybrid|on-?site|onsite|relocation", s):
        return "work_mode"
    if re.search(r"location|city|state", s):
        return "location"
    return "instruction"


def llm_window(jd_text: str, head: int = 4500, tail: int = 4500) -> str:
    """Text actually sent to the LLM: head + tail so late requirements survive.

    Deterministic analysis still uses the FULL string; this only bounds the
    model call.
    """
    text = jd_text or ""
    if len(text) <= head + tail:
        return text
    return text[:head] + "\n\n[...middle omitted; full text is analyzed separately...]\n\n" + text[-tail:]


def extract_requirement_records(jd_text: str) -> List[Dict[str, Any]]:
    """Deterministic requirement records over the complete JD. No LLM."""
    text = jd_text or ""
    # LICENSE_RULES / clearance / work-auth patterns are lowercase, same as
    # fit_score._norm. Scan the lowered copy; spans stay valid for ASCII.
    hay = text.lower()
    records: List[Dict[str, Any]] = []
    seen: set = set()

    def _add(
        *,
        raw: str,
        kind: str,
        priority: str,
        resume_controllable: bool,
        match_start: int = -1,
    ) -> None:
        key = (kind, raw.strip().lower())
        if not raw.strip() or key in seen:
            return
        seen.add(key)
        sentence = _sentence_at(text, match_start if match_start >= 0 else text.lower().find(raw.lower()))
        loc = _span(text, raw if match_start < 0 else text[match_start:match_start + max(12, len(raw))])
        records.append({
            "requirement_id": _req_id(raw, kind),
            "text": raw.strip(),
            "normalized_term": raw.strip(),
            "source_sentence": sentence,
            "source_location": loc,
            "requirement_type": kind,
            "priority": priority,
            "resume_controllable": resume_controllable,
            "evidence_status": "unknown",
            "confidence": 0.85 if loc else 0.55,
        })

    for rule in LICENSE_RULES:
        m = rule["jd"].search(hay)
        if m:
            _add(
                raw=rule["label"],
                kind="license",
                priority="mandatory",
                resume_controllable=True,
                match_start=m.start(),
            )

    m = _CLEARANCE_JD.search(hay)
    if m:
        _add(
            raw=m.group(0),
            kind="clearance",
            priority="mandatory",
            resume_controllable=False,
            match_start=m.start(),
        )

    m = _WORKAUTH_JD.search(hay)
    if m:
        _add(
            raw=m.group(0),
            kind="work_auth",
            priority="mandatory",
            resume_controllable=False,
            match_start=m.start(),
        )

    for m in _YEARS_RE.finditer(text):
        sentence = _sentence_at(text, m.start())
        if re.search(r"experience|years", sentence, re.IGNORECASE):
            _add(
                raw=m.group(0),
                kind="experience",
                priority=_priority_for_sentence(sentence),
                resume_controllable=False,
                match_start=m.start(),
            )
            break

    for m in _DEGREE_RE.finditer(text):
        sentence = _sentence_at(text, m.start())
        if re.search(r"degree|bachelor|master|phd|education", sentence, re.IGNORECASE):
            _add(
                raw=m.group(0),
                kind="education",
                priority=_priority_for_sentence(sentence),
                resume_controllable=True,
                match_start=m.start(),
            )
            break

    for m in _LOGISTICS_RE.finditer(text):
        sentence = _sentence_at(text, m.start())
        kind = _logistics_type(sentence)
        if kind in ("license",):
            continue
        _add(
            raw=m.group(0),
            kind=kind,
            priority="mandatory" if _KNOCKOUT_HINTS.search(sentence) else "contextual",
            resume_controllable=False,
            match_start=m.start(),
        )

    return records


def _records_from_skill_lists(jd_analysis: Dict[str, Any], jd_text: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for skill in jd_analysis.get("required_skills") or []:
        if not isinstance(skill, str) or not skill.strip():
            continue
        if _LOGISTICS_RE.search(skill):
            continue
        records.append({
            "requirement_id": _req_id(skill, "skill"),
            "text": skill.strip(),
            "normalized_term": skill.strip(),
            "source_sentence": _sentence_at(jd_text, jd_text.lower().find(skill.lower())),
            "source_location": _span(jd_text, skill),
            "requirement_type": "skill",
            "priority": "mandatory",
            "resume_controllable": True,
            "evidence_status": "unknown",
            "confidence": 0.7,
        })
    for skill in jd_analysis.get("preferred_skills") or []:
        if not isinstance(skill, str) or not skill.strip():
            continue
        if _LOGISTICS_RE.search(skill):
            continue
        records.append({
            "requirement_id": _req_id(skill, "skill_pref"),
            "text": skill.strip(),
            "normalized_term": skill.strip(),
            "source_sentence": _sentence_at(jd_text, jd_text.lower().find(skill.lower())),
            "source_location": _span(jd_text, skill),
            "requirement_type": "skill",
            "priority": "preferred",
            "resume_controllable": True,
            "evidence_status": "unknown",
            "confidence": 0.65,
        })
    return records


def strip_logistics_from_keywords(jd_analysis: Dict[str, Any]) -> Dict[str, Any]:
    """Remove work-auth / sponsorship terms from keyword/skill lists."""
    def _clean(values: Any) -> List[str]:
        out = []
        for v in values or []:
            if isinstance(v, str) and v.strip() and not _LOGISTICS_RE.search(v):
                out.append(v)
        return out

    jd_analysis["required_skills"] = _clean(jd_analysis.get("required_skills"))
    jd_analysis["preferred_skills"] = _clean(jd_analysis.get("preferred_skills"))
    jd_analysis["keywords"] = _clean(jd_analysis.get("keywords"))
    return jd_analysis


def annotate_evidence(
    requirements: List[Dict[str, Any]],
    resume: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Mark each requirement supported / missing against the resume text."""
    from schemas.resume_schemas import build_resume_text

    haystack = build_resume_text(resume or {}).lower()
    for req in requirements:
        term = (req.get("normalized_term") or req.get("text") or "").lower()
        if not term or not req.get("resume_controllable"):
            if not req.get("resume_controllable"):
                req["evidence_status"] = "unknown"
            continue
        tokens = [t for t in re.split(r"[^a-z0-9+.#]+", term) if len(t) > 2]
        if tokens and all(t in haystack for t in tokens[:2]):
            req["evidence_status"] = "supported"
        elif tokens and any(t in haystack for t in tokens):
            req["evidence_status"] = "partial"
        else:
            req["evidence_status"] = "missing"
    return requirements


def enrich(
    jd_analysis: Dict[str, Any],
    jd_text: str,
    resume: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach `requirements` to a validated JD analysis. Never drops flat lists.

    When ``jd_text`` is empty, existing ``requirements`` are kept and only
    evidence_status is refreshed against the resume.
    """
    jd_analysis = strip_logistics_from_keywords(dict(jd_analysis or {}))
    existing = list(jd_analysis.get("requirements") or [])
    if jd_text:
        records = extract_requirement_records(jd_text)
        records.extend(_records_from_skill_lists(jd_analysis, jd_text))
        by_id: Dict[str, Dict[str, Any]] = {}
        for rec in existing + records:
            if isinstance(rec, dict) and rec.get("requirement_id"):
                by_id.setdefault(rec["requirement_id"], rec)
        merged = list(by_id.values())
        jd_analysis["jd_char_count"] = len(jd_text)
    else:
        merged = [r for r in existing if isinstance(r, dict)]
        if not merged:
            merged = _records_from_skill_lists(jd_analysis, "")
    if resume:
        annotate_evidence(merged, resume)
    jd_analysis["requirements"] = merged
    return jd_analysis
