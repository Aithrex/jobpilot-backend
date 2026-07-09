"""`raw` provenance mode (§6.12.6) — a resume with ZERO model-generated tokens.

The user's own sentences, selected and lightly ordered, plus factual scaffolding
(contact/education/skills) drawn from their parsed resume and grounded graph. This
is the mode for the user who does not want a model touching their words at all, and
per the addendum it must exist and be excellent — and it *ships first*, because the
mode that generates nothing is the mode that proves the graph is right.

Everything here is deterministic. No LLM call. Every bullet carries provenance:
the sample it came from and the exact char span, so the UI can show its source.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from utils.db_connect import DBConnect

from services.voicegraph import samples as sample_svc
from services.voicegraph import store

logger = logging.getLogger(__name__)

MAX_BULLETS = 12
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_NUMBER = re.compile(r"\b\d[\d,.%kKmMbB]*\b")
_SENT = re.compile(r"[^.!?\n]+[.!?]?", re.UNICODE)
_STRONG_VERBS = {
    "shipped", "built", "cut", "traced", "rewrote", "led", "fixed", "designed",
    "wrote", "reduced", "removed", "migrated", "debugged", "launched", "scaled",
    "automated", "refactored", "deployed", "measured", "found", "solved", "tore",
}


def _col():
    return DBConnect().get_db().authored_resumes


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sentences_with_offsets(text: str) -> List[Tuple[str, int, int]]:
    out = []
    for m in _SENT.finditer(text):
        chunk = m.group()
        stripped = chunk.strip()
        if not stripped:
            continue
        lead = len(chunk) - len(chunk.lstrip())
        start = m.start() + lead
        out.append((stripped, start, start + len(stripped)))
    return out


def _score_sentence(sentence: str) -> float:
    words = _WORD.findall(sentence)
    n = len(words)
    if n < 5:
        return 0.0
    score = 0.0
    # length sweet spot 6..30 words
    if 6 <= n <= 30:
        score += 2.0
    elif n <= 40:
        score += 0.5
    else:
        score -= 1.0
    # concreteness: numbers + mid-sentence proper nouns
    score += min(6.0, 2.0 * len(_NUMBER.findall(sentence)))
    proper = sum(1 for i, w in enumerate(words) if i > 0 and w[0].isupper() and not w.isupper())
    score += min(3.0, 0.7 * proper)
    # a concrete action verb
    low = {w.lower() for w in words}
    if low & _STRONG_VERBS or any(w.lower().endswith("ed") and len(w) > 4 for w in words):
        score += 2.0
    # first person ownership ("I ...") reads as authentic experience
    if words and words[0].lower() == "i":
        score += 0.8
    return score


def _select_bullets(user_email: str) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for s in sample_svc.list_samples(user_email):
        if s.get("status") != "ready":
            continue
        sid = s.get("sample_id")
        for sent, start, end in _sentences_with_offsets(s.get("text", "")):
            sc = _score_sentence(sent)
            if sc <= 0:
                continue
            candidates.append({
                "text": sent, "score": sc, "source_id": sid,
                "char_start": start, "char_end": end,
            })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    # dedupe near-duplicates by a normalized prefix
    seen = set()
    picked = []
    for c in candidates:
        key = re.sub(r"\s+", " ", c["text"].lower())[:60]
        if key in seen:
            continue
        seen.add(key)
        picked.append(c)
        if len(picked) >= MAX_BULLETS:
            break
    return picked


def _skills_from_graph(user_email: str) -> List[str]:
    graph = store.get_graph(user_email)
    skills = []
    seen = set()
    for n in graph.get("nodes", []):
        if n.get("type") in ("Skill", "Technology"):
            label = (n.get("label") or "").strip()
            key = label.lower()
            if label and key not in seen:
                seen.add(key)
                skills.append(label)
    return skills


def _parsed_scaffold(user_email: str) -> Dict[str, Any]:
    """Contact + education from the parsed resume, if one exists. Best-effort."""
    try:
        from services.resume_parser import ResumeParser
        doc = ResumeParser().get_structured_resume(user_email=user_email)
        structured = (doc or {}).get("structured") or {}
        return {
            "contact": structured.get("contact", {}) or {},
            "education": structured.get("education", []) or [],
        }
    except Exception as e:
        logger.info("voicegraph raw: no parsed scaffold (%s)", e)
        return {"contact": {}, "education": []}


def build_raw_resume(user_email: str) -> Dict[str, Any]:
    """Assemble and persist a raw-mode resume. Returns structured doc + provenance.
    Raises ValueError('no_ready_samples') if there is nothing to build from."""
    bullets = _select_bullets(user_email)
    if not bullets:
        raise ValueError("no_ready_samples")

    scaffold = _parsed_scaffold(user_email)
    skills = _skills_from_graph(user_email)

    structured = {
        "contact": scaffold["contact"],
        "summary": "",  # raw mode never writes a summary (no fabrication)
        "skills": {"Skills": skills} if skills else {},
        "experience": [{
            "title": "Highlights",
            "company": "",
            "location": "",
            "dates": "",
            "type": "",
            "bullets": [b["text"] for b in bullets],
        }],
        "education": scaffold["education"],
        "projects": [],
        "certifications": [],
    }

    provenance = [{
        "bullet": b["text"],
        "mode": "verbatim",
        "source": "writing_sample",
        "source_id": b["source_id"],
        "char_start": b["char_start"],
        "char_end": b["char_end"],
    } for b in bullets]

    resume_id = str(uuid4())
    _col().insert_one({
        "resume_id": resume_id,
        "user_email": user_email,
        "provenance_mode": "raw",
        "structured": structured,
        "evidence_map": provenance,
        "voice_distance": 0.0,
        "style_profile_version": None,
        "created_at": _now(),
    })

    logger.info("voicegraph raw: built resume %s with %d verbatim bullets", resume_id, len(bullets))
    return {"resume_id": resume_id, "mode": "raw", "structured": structured, "provenance": provenance}


def list_resumes(user_email: str) -> List[Dict[str, Any]]:
    out = []
    for d in _col().find({"user_email": user_email}).sort("created_at", -1):
        out.append({
            "resume_id": d.get("resume_id"),
            "mode": d.get("provenance_mode"),
            "voice_distance": d.get("voice_distance"),
            "created_at": d["created_at"].isoformat() if hasattr(d.get("created_at"), "isoformat") else None,
            "bullet_count": len((d.get("structured") or {}).get("experience", [{}])[0].get("bullets", [])),
        })
    return out


def get_resume(user_email: str, resume_id: str) -> Optional[Dict[str, Any]]:
    d = _col().find_one({"resume_id": resume_id, "user_email": user_email})
    if not d:
        return None
    return {
        "resume_id": d.get("resume_id"),
        "mode": d.get("provenance_mode"),
        "structured": d.get("structured"),
        "provenance": d.get("evidence_map"),
        "voice_distance": d.get("voice_distance"),
        "style_profile_version": d.get("style_profile_version"),
    }
