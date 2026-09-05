"""Canonical requirement matching for assessments and gap reporting.

One implementation so ResumeScorer, KeywordGapEngine, requirement_assessment,
and match_gap_reporter agree on presence, aliases, and denominators.

Assessment-only: does not change tailoring prompts or synonym groups used by
the writer. Phrase-core extraction and semantic wording live here so a JD
label such as "REST API Development" can recognize "developed REST endpoints"
without treating generic API work as OpenAPI/Swagger.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from services.artifact_assessment import (
    normalize_preserve_boundaries,
    token_boundary_match,
)
from services.term_registry import match_forms, sanitize_jd_analysis
from utils.keyword_normalizer import normalize_keywords, normalize_single


MatchMethod = str  # exact | recognized_alias | semantic | none

# Trailing JD nouns that name a capability family, not a distinct tool.
_GENERIC_TAILS = re.compile(
    r"\s+(?:development|engineering|experience|knowledge|skills?|"
    r"proficiency|programming|implementation)\s*$",
    re.I,
)
_GENERIC_HEADS = re.compile(
    r"^(?:experience (?:with|in)|knowledge of|proficient in|familiar with|"
    r"working (?:with|knowledge of)|strong|hands[- ]on)\s+",
    re.I,
)

# Slash compounds that are one term, not OR alternatives.
_SLASH_ATOMIC = frozenset({
    "ci/cd", "ci/cd pipelines", "tcp/ip", "os/2", "i/o", "a/b",
    "c/c++", "s3", "r/r",
})

# Related technologies that must never count as aliases of each other.
_DISTINCT_PAIRS = (
    ("sql", "nosql"),
    ("java", "javascript"),
    ("react", "angular"),
    ("aws", "azure"),
    ("python", "javascript"),
)


@dataclass
class TermMatch:
    found: bool
    method: MatchMethod
    matched_form: str = ""
    span: str = ""
    section: str = ""
    in_denominator: bool = True
    aliases: List[str] = field(default_factory=list)


@dataclass
class RequirementDiagnostic:
    jd_text: str
    classification: str  # required | preferred | context | keyword
    canonical_identity: str
    accepted_aliases: List[str]
    matched_span: str
    matched_section: str
    source_support: str
    match_method: MatchMethod
    unmet_qualifiers: List[str]
    in_denominator: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "jd_text": self.jd_text,
            "classification": self.classification,
            "canonical_identity": self.canonical_identity,
            "accepted_aliases": list(self.accepted_aliases),
            "matched_span": self.matched_span,
            "matched_section": self.matched_section,
            "source_support": self.source_support,
            "match_method": self.match_method,
            "unmet_qualifiers": list(self.unmet_qualifiers),
            "in_denominator": self.in_denominator,
        }


def normalize_term(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = t.replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"[\"'`“”‘’]", "", t)
    return t.strip()


def canonical_identity(term: str) -> str:
    raw = normalize_term(term)
    return (normalize_single(raw) or raw).strip().lower()


def phrase_cores(term: str) -> List[str]:
    """JD phrase plus stripped capability cores used only for matching."""
    raw = normalize_term(term)
    if not raw:
        return []
    out: List[str] = [raw]
    stripped = _GENERIC_HEADS.sub("", raw).strip()
    stripped = _GENERIC_TAILS.sub("", stripped).strip()
    if stripped and stripped.casefold() != raw.casefold():
        out.append(stripped)
    # "REST API Development" → "REST API"; keep shorter known cores.
    parts = stripped.split()
    if len(parts) >= 2:
        out.append(" ".join(parts[:2]))
    return list(dict.fromkeys(out))


def slash_alternatives(term: str) -> List[str]:
    raw = normalize_term(term)
    if not raw or "/" not in raw:
        return [raw] if raw else []
    if raw.casefold() in _SLASH_ATOMIC:
        return [raw]
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    if len(parts) >= 2 and all(1 <= len(p.split()) <= 3 for p in parts):
        return parts
    return [raw]


def accepted_aliases(term: str) -> List[str]:
    """Deduplicated alias list: parentheticals, registry forms, phrase cores."""
    seen: Set[str] = set()
    out: List[str] = []

    def _add(piece: str) -> None:
        piece = normalize_term(piece)
        if not piece:
            return
        key = piece.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(piece)
        for form in match_forms(piece):
            f = normalize_term(form)
            fk = f.casefold()
            if f and fk not in seen:
                seen.add(fk)
                out.append(f)

    for alt in slash_alternatives(term):
        for core in phrase_cores(alt):
            _add(core)
            canon = normalize_single(core)
            if canon:
                _add(canon)
    return out


def _span_around(text: str, needle: str, window: int = 140) -> str:
    if not text or not needle:
        return ""
    m = re.search(re.escape(needle), text, re.I)
    if not m:
        # needle may be a normalized form; search casefold
        low = text.casefold()
        n = needle.casefold()
        i = low.find(n)
        if i < 0:
            return needle
        start = max(0, i - window)
        end = min(len(text), i + len(needle) + window)
        return text[start:end]
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    return text[start:end]


def match_term_in_text(text: str, term: str) -> TermMatch:
    """Word-boundary match of a JD term against document text."""
    aliases = accepted_aliases(term)
    if not text or not term:
        return TermMatch(False, "none", aliases=aliases)
    identity = canonical_identity(term)
    for form in aliases:
        if not token_boundary_match(text, form):
            continue
        method: MatchMethod = (
            "exact" if form.casefold() == identity or form.casefold() == normalize_term(term).casefold()
            else "recognized_alias"
        )
        # Phrase-core / stripped-tail hits are semantic, not a new technology.
        if form.casefold() not in {normalize_term(term).casefold(), identity}:
            cores = {c.casefold() for c in phrase_cores(term)}
            if form.casefold() in cores or any(
                token_boundary_match(form, c) for c in cores
            ):
                if form.casefold() != identity:
                    method = "semantic" if method != "recognized_alias" else method
        span = _span_around(text, form)
        return TermMatch(
            True, method, matched_form=form, span=span, aliases=aliases,
        )
    return TermMatch(False, "none", aliases=aliases)


def locate_term_in_resume(resume: Optional[Dict[str, Any]], term: str) -> TermMatch:
    """Find a term in visible resume sections (projects, certs, education included)."""
    if not resume:
        return match_term_in_text("", term)
    from schemas.resume_schemas import build_resume_text, flatten_skills

    sections: List[Tuple[str, str]] = []
    if resume.get("summary"):
        sections.append(("summary", str(resume.get("summary") or "")))
    skills = flatten_skills(resume)
    if skills:
        sections.append(("skills", ", ".join(str(s) for s in skills)))
    for i, exp in enumerate(resume.get("experience") or []):
        blob = "\n".join(
            [str(exp.get("title") or ""), str(exp.get("company") or "")]
            + [str(b) for b in (exp.get("bullets") or [])]
        )
        sections.append((f"experience.{i}", blob))
    for i, proj in enumerate(resume.get("projects") or []):
        blob = "\n".join(
            [str(proj.get("name") or "")]
            + [str(b) for b in (proj.get("bullets") or [])]
        )
        sections.append((f"projects.{i}", blob))
    for i, edu in enumerate(resume.get("education") or []):
        blob = " ".join(
            str(edu.get(k) or "") for k in ("degree", "institution", "coursework")
        )
        sections.append((f"education.{i}", blob))
    for i, cert in enumerate(resume.get("certifications") or []):
        sections.append((f"certifications.{i}", str(cert)))

    full = build_resume_text(resume)
    best = match_term_in_text(full, term)
    if not best.found:
        return best
    for section, blob in sections:
        hit = match_term_in_text(blob, term)
        if hit.found:
            hit.section = section
            hit.aliases = best.aliases
            return hit
    best.section = "document"
    return best


def distinct_technologies(a: str, b: str) -> bool:
    """True when a and b are related-but-not-equivalent technologies."""
    na, nb = canonical_identity(a), canonical_identity(b)
    for x, y in _DISTINCT_PAIRS:
        if {na, nb} == {x, y}:
            return True
        if (na == x and nb.startswith(y)) or (nb == x and na.startswith(y)):
            return True
    return False


def dedupe_synonym_terms(terms: Sequence[str]) -> List[str]:
    """Keep one representative per alias group (acronym + expansion = one)."""
    kept: List[str] = []
    seen_ids: Set[str] = set()
    for raw in terms:
        if not isinstance(raw, str) or not raw.strip():
            continue
        identity = canonical_identity(raw)
        alias_keys = {canonical_identity(f) for f in accepted_aliases(raw)}
        alias_keys.add(identity)
        if seen_ids & alias_keys:
            continue
        seen_ids.update(alias_keys)
        kept.append(raw.strip())
    return kept


def scored_term_lists(jd_analysis: Dict[str, Any]) -> Dict[str, List[str]]:
    """Required / preferred / keywords / context — no domain promotion."""
    jd = sanitize_jd_analysis(dict(jd_analysis or {}))
    required = dedupe_synonym_terms(normalize_keywords(jd.get("required_skills") or []))
    preferred = dedupe_synonym_terms(normalize_keywords(jd.get("preferred_skills") or []))
    keywords = dedupe_synonym_terms(normalize_keywords(jd.get("keywords") or []))
    ctx = jd.get("jd_context") or {}
    domain = list(ctx.get("domain") or [])
    # Preferred and domain never enter the required denominator.
    req_set = {canonical_identity(t) for t in required}
    keywords = [k for k in keywords if canonical_identity(k) not in req_set]
    pref_set = {canonical_identity(t) for t in preferred}
    keywords = [k for k in keywords if canonical_identity(k) not in pref_set]
    return {
        "required": required,
        "preferred": preferred,
        "keywords": keywords,
        "domain": domain,
    }


def coverage_hits(
    text: str,
    terms: Sequence[str],
    *,
    resume: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], List[str], List[TermMatch]]:
    """Presence-only coverage. Repetition does not change the result."""
    found: List[str] = []
    missing: List[str] = []
    matches: List[TermMatch] = []
    for term in terms:
        hit = locate_term_in_resume(resume, term) if resume else match_term_in_text(text, term)
        if not resume:
            hit = match_term_in_text(text, term)
        matches.append(hit)
        canon = canonical_identity(term)
        if hit.found:
            found.append(canon)
        else:
            missing.append(canon)
    return found, missing, matches


def coverage_score(found: Sequence[str], missing: Sequence[str]) -> Optional[int]:
    """Internal document coverage 0–100, or None when the denominator is 0."""
    total = len(found) + len(missing)
    if total == 0:
        return None
    return round((len(found) / total) * 100)


def predicate_identity(
    capability: str,
    *,
    action: str = "",
    context: str = "",
    ownership_required: bool = False,
    duration_required: bool = False,
    currency_required: bool = False,
    evidence_expectation: str = "",
) -> str:
    """Full predicate key — qualifiers distinguish otherwise identical capabilities."""
    return "|".join((
        canonical_identity(capability),
        (action or "").strip().lower(),
        (context or "").strip().lower(),
        "own" if ownership_required else "",
        "dur" if duration_required else "",
        "cur" if currency_required else "",
        (evidence_expectation or "").strip().lower(),
    ))


def build_requirement_diagnostics(
    *,
    jd_analysis: Dict[str, Any],
    document_text: str,
    resume: Optional[Dict[str, Any]] = None,
    source_text: str = "",
    source_available: bool = False,
) -> List[RequirementDiagnostic]:
    lists = scored_term_lists(jd_analysis)
    rows: List[RequirementDiagnostic] = []
    for classification, terms, in_den in (
        ("required", lists["required"], True),
        ("preferred", lists["preferred"], False),
        ("keyword", lists["keywords"], False),
        ("context", lists["domain"], False),
    ):
        for term in terms:
            doc = locate_term_in_resume(resume, term) if resume else match_term_in_text(
                document_text, term,
            )
            if resume and document_text and not doc.found:
                doc = match_term_in_text(document_text, term)
            source_state = "unknown"
            if source_available:
                src = match_term_in_text(source_text, term)
                source_state = "supported" if src.found else "not_evidenced"
            rows.append(RequirementDiagnostic(
                jd_text=term,
                classification=classification,
                canonical_identity=canonical_identity(term),
                accepted_aliases=doc.aliases,
                matched_span=doc.span,
                matched_section=doc.section,
                source_support=source_state,
                match_method=doc.method if doc.found else "none",
                unmet_qualifiers=[],
                in_denominator=in_den,
            ))
    return rows
