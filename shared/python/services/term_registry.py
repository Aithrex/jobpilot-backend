"""Shared term registry — classify, expand, and match JD/resume terms.

One implementation so extract_jd, KeywordGapEngine, ResumeScorer, and the
UI inherit the same answers for "is this a skill?" and "does the resume
contain it?".
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Set, Tuple

from utils.keyword_normalizer import (
    get_all_forms,
    is_programming_language,
    normalize_single,
    taxonomy_group,
)

logger = logging.getLogger(__name__)

TermClass = Literal["skill", "domain", "logistics", "seniority", "title"]

# ---------------------------------------------------------------------------
# Classification lists
# ---------------------------------------------------------------------------

LOGISTICS = frozenset({
    "contract w2", "contract", "hybrid", "remote", "on-site", "onsite",
    "on site", "full-time", "part-time", "fulltime", "parttime", "w2", "1099",
    "us citizen", "green card", "h1b", "h-1b", "visa sponsorship",
    "no sponsorship", "work authorization", "available for", "open to",
    "relocation", "travel required",
})

SENIORITY = frozenset({
    "entry level", "entry-level", "new grad", "new graduate", "junior",
    "intern", "internship", "mid level", "mid-level", "senior", "staff",
    "principal", "lead", "director", "distinguished", "fellow",
    "engineer i", "engineer ii", "engineer iii", "engineer iv",
})

TITLES = frozenset({
    "software engineer", "software engineering", "software developer",
    "software development", "backend", "frontend", "fullstack",
    "full-stack", "full stack", "back-end", "front-end",
    "full-stack development", "fullstack development", "full stack development",
    "backend development", "frontend development",
    "full-stack developer", "fullstack developer", "full stack developer",
    "backend engineer", "frontend engineer",
    "full stack engineer", "fullstack engineer", "full-stack engineer",
    "backend services", "backend services development",
    "data engineer", "data scientist", "data analyst",
    "product manager", "project manager", "program manager",
})

DOMAINS = frozenset({
    "fintech", "finance", "financial services", "payments", "banking",
    "healthcare", "health care", "clinical", "biotech",
    "gaming", "game", "robotics", "automotive", "ecommerce", "e-commerce",
    "edtech", "education", "adtech", "media", "advertising",
    "saas", "hr tech", "hrtech", "insurtech", "regtech",
    "government", "defense", "public sector",
})

# Umbrella phrases → any member satisfies the phrase.
UMBRELLA: Dict[str, Tuple[str, ...]] = {
    "relational databases": (
        "postgresql", "mysql", "sql server", "sqlite", "oracle",
        "mariadb", "amazon rds", "rds",
    ),
    "relational database": (
        "postgresql", "mysql", "sql server", "sqlite", "oracle",
        "mariadb", "amazon rds", "rds",
    ),
    "cloud platforms": ("aws", "google cloud platform", "microsoft azure"),
    "cloud platform": ("aws", "google cloud platform", "microsoft azure"),
    "ci/cd": ("github actions", "gitlab ci", "jenkins", "circleci", "travis ci"),
    "ci/cd pipelines": ("github actions", "gitlab ci", "jenkins", "circleci"),
    "infrastructure as code": ("terraform", "pulumi", "cloudformation", "ansible"),
}

# Short language names: always word-bounded; never substring.
_SHORT_LANG_EXTRA: Dict[str, Tuple[str, ...]] = {
    "go": ("golang", "go lang", "go language"),
    "r": ("r language", "r lang", "r programming", "r studio"),
    "c": ("c language", "c programming"),
    "c#": ("csharp", "c sharp", "c-sharp"),
    "c++": ("cpp", "cplusplus", "c plus plus", "c-plus-plus"),
}

_PAREN_SPLIT = re.compile(r"\s*(?:,|/|;|\bor\b|\band\b)\s*", re.IGNORECASE)
_PAREN_TERM = re.compile(r"^(.*?)\s*\((.*)\)\s*$")
_WS = re.compile(r"\s+")


def _norm(term: str) -> str:
    return _WS.sub(" ", str(term or "").strip().lower())


def classify_term(
    term: str,
    *,
    company: str = "",
    industry: str = "",
) -> TermClass:
    """Classify a JD-extracted string. Default is skill (unknown ≠ junk)."""
    raw = _norm(term)
    if not raw:
        return "title"
    if company and raw == _norm(company):
        return "title"
    if industry:
        for part in industry.replace("/", ",").split(","):
            if raw == _norm(part) or raw in _norm(part).split():
                if raw in DOMAINS or raw == _norm(part):
                    return "domain"
    if raw in LOGISTICS:
        return "logistics"
    if raw in SENIORITY:
        return "seniority"
    if raw in TITLES:
        return "title"
    if raw in DOMAINS:
        return "domain"
    if raw.endswith(" engineer") or raw.endswith(" developer"):
        return "title"
    if raw.endswith(" development") and raw.replace(" development", "") in TITLES:
        return "title"
    try:
        from services.fabrication_filter import is_nonskill_soft_trait
        if is_nonskill_soft_trait(term):
            return "title"
    except Exception:
        pass
    if len(raw.split()) > 6:
        return "title"
    return "skill"


def expand_parenthetical(term: str) -> List[str]:
    """`X (a, b, or c)` → [X, a, b, c]. Bare terms return [term]."""
    raw = str(term or "").strip()
    if not raw:
        return []
    m = _PAREN_TERM.match(raw)
    if not m:
        return [raw]
    head = m.group(1).strip()
    inner = m.group(2).strip()
    parts = [p.strip() for p in _PAREN_SPLIT.split(inner) if p.strip()]
    out: List[str] = []
    if head:
        out.append(head)
    out.extend(parts)
    return out or [raw]


def match_forms(term: str) -> List[str]:
    """All strings that satisfy `term`: aliases, parentheticals, umbrella members."""
    seen: Set[str] = set()
    out: List[str] = []

    def _add(piece: str) -> None:
        piece = (piece or "").strip()
        if not piece:
            return
        key = _norm(piece)
        if key in seen:
            return
        seen.add(key)
        out.append(piece)
        for form in get_all_forms(piece):
            if form and form not in seen:
                seen.add(form)
                out.append(form)
        extra = _SHORT_LANG_EXTRA.get(key)
        if extra:
            for f in extra:
                if f not in seen:
                    seen.add(f)
                    out.append(f)
        members = UMBRELLA.get(key)
        if members:
            for m in members:
                _add(m)

    for piece in expand_parenthetical(term):
        _add(piece)
        canon = normalize_single(piece)
        if canon and _norm(canon) != _norm(piece):
            _add(canon)
    return out


def text_contains(text_lower: str, keyword: str) -> bool:
    """Word-boundary match at every length. Short languages never substring."""
    if not keyword or not text_lower:
        return False
    blob = text_lower if text_lower == text_lower.lower() else text_lower.lower()
    for form in match_forms(keyword):
        f = form.lower().strip()
        if not f:
            continue
        if re.search(r"(?<![a-zA-Z])" + re.escape(f) + r"(?![a-zA-Z])", blob):
            return True
    return False


def term_in_structured(
    term: str,
    structured: Dict[str, Any],
    *,
    include_skills: bool = True,
) -> bool:
    """True if `term` is evidenced on the structured resume."""
    from schemas.resume_schemas import build_resume_text, flatten_skills

    if include_skills:
        for skill in flatten_skills(structured) or []:
            if text_contains(str(skill).lower(), term) or text_contains(
                _norm(term), str(skill)
            ):
                return True
            if _norm(normalize_single(skill) or skill) == _norm(
                normalize_single(term) or term
            ):
                return True
    hay = (build_resume_text(structured) or "").lower()
    if text_contains(hay, term):
        return True
    for cert in structured.get("certifications") or []:
        if text_contains(str(cert).lower(), term):
            return True
    return False


def context_text(structured: Dict[str, Any]) -> str:
    """Summary + experience/project bullets — demonstrated use, not a list."""
    parts: List[str] = [structured.get("summary") or ""]
    for exp in structured.get("experience") or []:
        parts.extend(b for b in (exp.get("bullets") or []) if isinstance(b, str))
    for proj in structured.get("projects") or []:
        parts.extend(b for b in (proj.get("bullets") or []) if isinstance(b, str))
    return "\n".join(p for p in parts if p).lower()


def evidence_weight(term: str, structured: Dict[str, Any]) -> float:
    """1.0 in a bullet/summary, 0.6 skills+elsewhere, 0.0 skills-list only."""
    ctx = context_text(structured)
    if text_contains(ctx, term):
        return 1.0
    from schemas.resume_schemas import flatten_skills
    in_skills = any(
        text_contains(str(s).lower(), term)
        or _norm(normalize_single(s) or s) == _norm(normalize_single(term) or term)
        for s in (flatten_skills(structured) or [])
    )
    if not in_skills:
        if text_contains((structured.get("summary") or "").lower(), term):
            return 1.0
        for cert in structured.get("certifications") or []:
            if text_contains(str(cert).lower(), term):
                return 0.6
        return 0.0
    # Skills list: only 0.6 if also in education/certs/project name (not tech).
    extras = []
    for edu in structured.get("education") or []:
        extras.append(edu.get("coursework") or "")
        extras.append(edu.get("degree") or "")
    extras.extend(str(c) for c in (structured.get("certifications") or []))
    for proj in structured.get("projects") or []:
        extras.append(proj.get("name") or "")
        extras.extend(b for b in (proj.get("bullets") or []) if isinstance(b, str))
    extra_blob = "\n".join(extras).lower()
    if text_contains(extra_blob, term):
        return 0.6
    return 0.0


def partition_jd_terms(
    terms: Sequence[str],
    *,
    company: str = "",
    industry: str = "",
) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {
        "skill": [], "domain": [], "logistics": [], "seniority": [], "title": [],
    }
    seen: Set[str] = set()
    for raw in terms:
        if not isinstance(raw, str) or not raw.strip():
            continue
        key = _norm(normalize_single(raw) or raw)
        if key in seen:
            continue
        seen.add(key)
        klass = classify_term(raw, company=company, industry=industry)
        buckets[klass].append(raw.strip())
    return buckets


def sanitize_jd_analysis(jd: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only skills in scored lists; park the rest on `jd_context`."""
    if not isinstance(jd, dict):
        return jd
    company = str(jd.get("company") or "")
    industry = str(jd.get("industry") or "")
    ctx = {
        "domain": [],
        "logistics": [],
        "seniority": [],
        "title": [],
    }
    for field in ("required_skills", "preferred_skills", "keywords"):
        parts = partition_jd_terms(
            jd.get(field) or [], company=company, industry=industry,
        )
        jd[field] = parts["skill"]
        for k in ("domain", "logistics", "seniority", "title"):
            ctx[k].extend(x for x in parts[k] if x not in ctx[k])
    existing = jd.get("jd_context") if isinstance(jd.get("jd_context"), dict) else {}
    for k, vals in ctx.items():
        prev = list(existing.get(k) or [])
        jd_ctx_vals = list(dict.fromkeys(prev + vals))
        ctx[k] = jd_ctx_vals
    jd["jd_context"] = ctx
    dropped = sum(len(v) for v in ctx.values())
    if dropped:
        logger.info(
            "term_registry: parked %d non-skill JD term(s) in jd_context",
            dropped,
        )
    return jd


def skill_term_ceiling(years: int, *, has_roles: bool = False) -> int:
    """Max credible skills-list size: min(45, 6×years + 15).

    Unknown years with dated roles → hard cap only. No roles at all → 15
    (a new-grad inventory of 60 tools is the failure mode).
    """
    try:
        yrs = int(years)
    except (TypeError, ValueError):
        yrs = 0
    if yrs > 0:
        return min(45, 6 * yrs + 15)
    if has_roles:
        return 45
    return 15


def count_occurrences(text_lower: str, keyword: str) -> int:
    if not keyword or not text_lower:
        return 0
    blob = text_lower.lower()
    total = 0
    seen_forms: Set[str] = set()
    for form in match_forms(keyword):
        f = form.lower().strip()
        if not f or f in seen_forms:
            continue
        seen_forms.add(f)
        total += len(re.findall(
            r"(?<![a-zA-Z])" + re.escape(f) + r"(?![a-zA-Z])", blob,
        ))
    return total
