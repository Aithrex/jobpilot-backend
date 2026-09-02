"""
Claim Guard — deterministic numeric/entity grounding audit (zero AI).

Runs on the FINAL tailored text (post-augmenter, post-normalizers) so it
catches invented numbers regardless of which LLM pass introduced them —
the tailor's rewrite or the augmenter's impact injection are
indistinguishable to the user and equally in need of review.

Policy (per entity class):
- Invented certifications / employers / titles / dates / degrees: already
  STRIPPED deterministically by IntegrityGuard + fabrication_filter — this
  module does not duplicate that floor.
- Ungrounded METRICS: FLAG (never silently strip — the number may be true
  but undocumented; the user is the only arbiter). In strict mode, safely
  removable trailing metric clauses are downgraded to the unnumbered form.
- JD-named terms woven into bullets with no grounding in the source resume:
  FLAG (`unsupported_product`) — defensible-adjacent additions are a
  judgment call the review UI surfaces, not something regex should delete.

Mode: CLAIM_GUARD_MODE env — "flag" (default) or "strict" (apply safe
downgrades; also intended to disable augmenter metric-invention).

Never weakens integrity_guard/fabrication_filter — this layers ON TOP.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from schemas.resume_schemas import build_resume_text
from utils.keyword_normalizer import get_all_forms

logger = logging.getLogger(__name__)


def guard_mode() -> str:
    mode = os.getenv("CLAIM_GUARD_MODE", "flag").strip().lower()
    return mode if mode in ("flag", "strict") else "flag"


# ---------------------------------------------------------------------------
# Numeric token extraction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NumClaim:
    raw: str                # verbatim token as written ("40%", "$1.2M", "420ms")
    value_norm: str         # normalized numeric value ("40", "1.2", "420")
    unit_class: str         # percent|currency|multiplier|duration|scaled|count|plus
    span: Tuple[int, int]   # character span in the source string


# Order matters: more specific first so "420ms" isn't consumed as a bare count.
_NUM_PATTERNS: Tuple[Tuple[str, re.Pattern], ...] = (
    ("currency", re.compile(r"\$[\d,]+(?:\.\d+)?\s?[KMB]?\b", re.IGNORECASE)),
    ("percent", re.compile(r"\d+(?:\.\d+)?\s?%")),
    ("multiplier", re.compile(r"\b\d+(?:\.\d+)?x\b", re.IGNORECASE)),
    ("duration", re.compile(
        r"\b\d+(?:\.\d+)?\s?(?:ms|milliseconds?|s|secs?|seconds?|mins?|minutes?|"
        r"hrs?|hours?|days?|weeks?|months?|years?)\b",
        re.IGNORECASE,
    )),
    ("scaled", re.compile(r"\b\d+(?:\.\d+)?\s?[KMB]\b")),
    ("plus", re.compile(r"\b\d+(?:\.\d+)?\+")),
    ("count", re.compile(r"\b\d+(?:,\d{3})+\b|\b\d+(?:\.\d+)?\b")),
)

_VALUE_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _normalize_value(raw: str) -> str:
    m = _VALUE_RE.search(raw)
    if not m:
        return raw
    value = m.group(0).replace(",", "")
    # "40.0" and "40" ground each other.
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value


def extract_numbers(text: str) -> List[NumClaim]:
    """Extract every numeric token with its unit class, non-overlapping."""
    if not text:
        return []
    claims: List[NumClaim] = []
    taken: List[Tuple[int, int]] = []
    for unit_class, pattern in _NUM_PATTERNS:
        for m in pattern.finditer(text):
            span = m.span()
            if any(s < span[1] and span[0] < e for s, e in taken):
                continue
            taken.append(span)
            claims.append(NumClaim(
                raw=m.group(0),
                value_norm=_normalize_value(m.group(0)),
                unit_class=unit_class,
                span=span,
            ))
    claims.sort(key=lambda c: c.span[0])
    return claims


# ---------------------------------------------------------------------------
# Grounding index
# ---------------------------------------------------------------------------

@dataclass
class GroundingIndex:
    """Everything a tailored number/term may legitimately resolve against."""
    number_keys: Set[Tuple[str, str]] = field(default_factory=set)  # (value, unit)
    bare_values: Set[str] = field(default_factory=set)              # unit-agnostic
    years: Set[str] = field(default_factory=set)                    # from date fields
    source_text_lower: str = ""

    def grounds(self, claim: NumClaim) -> bool:
        if (claim.value_norm, claim.unit_class) in self.number_keys:
            return True
        # Same value with a different-but-compatible unit spelling still
        # grounds ("95 ms" vs "95ms" both normalize; "40 percent" edge).
        if claim.value_norm in self.bare_values:
            return True
        # Calendar years and tenure math derived from immutable dates.
        if claim.unit_class in ("count", "duration", "plus") and claim.value_norm in self.years:
            return True
        return False


def _iter_source_texts(structured: Dict[str, Any]) -> List[str]:
    texts = [structured.get("summary") or ""]
    for exp in structured.get("experience") or []:
        texts.extend(b for b in (exp.get("bullets") or []) if isinstance(b, str))
    for proj in structured.get("projects") or []:
        texts.extend(b for b in (proj.get("bullets") or []) if isinstance(b, str))
        texts.append(proj.get("tech") or "")
    for edu in structured.get("education") or []:
        texts.append(edu.get("coursework") or "")
        texts.append(edu.get("gpa") or "")
    return [t for t in texts if t]


def build_grounding_index(
    structured_resume: Dict[str, Any],
    extra_texts: Optional[List[str]] = None,
) -> GroundingIndex:
    """Index every number in the ORIGINAL resume (+ confirmed enrichment
    texts) so tailored numbers can be resolved against real facts."""
    index = GroundingIndex()
    texts = _iter_source_texts(structured_resume) + list(extra_texts or [])
    for text in texts:
        for claim in extract_numbers(text):
            index.number_keys.add((claim.value_norm, claim.unit_class))
            index.bare_values.add(claim.value_norm)

    # Years (and their spans) from immutable date fields — "4 years" tenure
    # claims and bare "2022" mentions ground against these.
    year_re = re.compile(r"\b(19|20)\d{2}\b")
    year_ints: List[int] = []
    for section in ("experience", "education"):
        for entry in structured_resume.get(section) or []:
            for m in year_re.finditer(entry.get("dates") or ""):
                index.years.add(m.group(0))
                year_ints.append(int(m.group(0)))
    if year_ints:
        # Tenure spans a candidate could honestly cite ("3 years", "5+ years").
        span = max(year_ints) - min(year_ints)
        for tenure in range(1, max(span, 1) + 2):
            index.years.add(str(tenure))

    index.source_text_lower = build_resume_text(structured_resume).lower()
    return index


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@dataclass
class ClaimAudit:
    """Flags keyed by structural address (experience.0.bullets.2, summary.s1)."""
    items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    downgraded: int = 0

    def flag(self, address: str, text: str, flag: str, claim_raw: str) -> None:
        entry = self.items.setdefault(
            address, {"text": text, "flags": [], "unsupported_claims": []}
        )
        if flag not in entry["flags"]:
            entry["flags"].append(flag)
        if claim_raw and claim_raw not in entry["unsupported_claims"]:
            entry["unsupported_claims"].append(claim_raw)
        entry["text"] = text


# A number glued to a grounded skill token is a version, not a metric
# ("Python 3", "HTTP/2", "ES6", "Angular 17").
_VERSION_CONTEXT_RE = re.compile(r"[A-Za-z][A-Za-z+#.]*[\s/\-]?$")

# Trailing adverbial metric clause that can be dropped without leaving a
# grammatical stump: ", reducing latency by 40%" / ", serving 5M users".
_SAFE_CLAUSE_RE = re.compile(
    r",\s*(?:reducing|improving|increasing|cutting|boosting|serving|handling|"
    r"processing|supporting|saving|lowering|raising|accelerating|driving)\b[^,]*\d[^,]*$",
    re.IGNORECASE,
)
# Mid-sentence "by 40%" phrase attached to a verb.
_BY_PHRASE_RE = re.compile(r"\s+by\s+(?:about\s+|under\s+|over\s+)?\d[\w.,%$]*")


def _is_version_number(text: str, claim: NumClaim) -> bool:
    prefix = text[: claim.span[0]]
    return bool(_VERSION_CONTEXT_RE.search(prefix[-24:])) and claim.unit_class in ("count",)


def safe_downgrade(bullet: str, claim: NumClaim) -> Optional[str]:
    """Remove an ungrounded metric ONLY when the clause shape allows clean
    surgery; None when no safe rewrite exists (caller keeps text + flag)."""
    if claim.raw not in bullet:
        return None
    clause = _SAFE_CLAUSE_RE.search(bullet)
    if clause and claim.span[0] >= clause.start():
        candidate = bullet[: clause.start()].rstrip()
        if len(candidate) >= 40:
            return candidate if candidate.endswith(".") else candidate + "."
    for m in _BY_PHRASE_RE.finditer(bullet):
        if m.start() <= claim.span[0] < m.end():
            candidate = (bullet[: m.start()] + bullet[m.end():]).strip()
            candidate = re.sub(r"\s{2,}", " ", candidate)
            if len(candidate) >= 40:
                return candidate
    return None


def _enumerate_items(tailored: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(address, text) for every reviewable line — bullets + summary sentences."""
    items: List[Tuple[str, str]] = []
    summary = (tailored.get("summary") or "").strip()
    if summary:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if s.strip()]
        for k, sentence in enumerate(sentences):
            items.append((f"summary.s{k}", sentence))
    for i, exp in enumerate(tailored.get("experience") or []):
        for j, bullet in enumerate(exp.get("bullets") or []):
            if isinstance(bullet, str) and bullet.strip():
                items.append((f"experience.{i}.bullets.{j}", bullet))
    for i, proj in enumerate(tailored.get("projects") or []):
        for j, bullet in enumerate(proj.get("bullets") or []):
            if isinstance(bullet, str) and bullet.strip():
                items.append((f"projects.{i}.bullets.{j}", bullet))
    return items


def _jd_terms_to_watch(jd_analysis: Dict[str, Any]) -> List[str]:
    terms = list(jd_analysis.get("required_skills") or [])
    terms += list(jd_analysis.get("keywords") or [])
    # Multi-word / product-like terms are the verifiable claims; single
    # generic words ("Python") produce too much noise to flag here — the
    # entailment verifier judges those in context.
    return [t for t in dict.fromkeys(terms) if isinstance(t, str) and len(t) >= 3]


def audit_resume(
    tailored: Dict[str, Any],
    structured_resume: Dict[str, Any],
    jd_analysis: Optional[Dict[str, Any]] = None,
    extra_grounding_texts: Optional[List[str]] = None,
    mode: Optional[str] = None,
) -> Tuple[Dict[str, Any], ClaimAudit]:
    """Audit the FINAL tailored resume against the source facts.

    Returns (possibly-downgraded tailored, ClaimAudit). In "flag" mode the
    resume text is never modified; in "strict" mode safely removable
    ungrounded metric clauses are downgraded to their unnumbered form.
    """
    mode = mode or guard_mode()
    index = build_grounding_index(structured_resume, extra_grounding_texts)
    audit = ClaimAudit()

    watch_terms = _jd_terms_to_watch(jd_analysis or {})
    source_lower = index.source_text_lower

    for address, text in _enumerate_items(tailored):
        current = text

        # --- Ungrounded metrics ---
        for claim in extract_numbers(current):
            if index.grounds(claim):
                continue
            if _is_version_number(current, claim):
                continue
            if mode == "strict":
                downgraded = safe_downgrade(current, claim)
                if downgraded:
                    logger.info(
                        "claim_guard strict: downgraded ungrounded metric %r in %s",
                        claim.raw, address,
                    )
                    current = downgraded
                    audit.downgraded += 1
                    audit.flag(address, current, "downgraded_metric", claim.raw)
                    continue
            audit.flag(address, current, "ungrounded_metric", claim.raw)

        # --- JD terms woven into bullets with no source grounding ---
        if watch_terms and not address.startswith("summary"):
            text_lower = current.lower()
            for term in watch_terms:
                forms = get_all_forms(term)
                in_bullet = any(_word_bounded(text_lower, f) for f in forms)
                if not in_bullet:
                    continue
                in_source = any(_word_bounded(source_lower, f) for f in forms)
                if not in_source:
                    audit.flag(address, current, "unsupported_product", term)

        if current != text:
            _write_back(tailored, address, current)

    return tailored, audit


def _word_bounded(text_lower: str, keyword_lower: str) -> bool:
    if not keyword_lower:
        return False
    if len(keyword_lower) <= 2:
        return keyword_lower in text_lower
    pattern = re.compile(r"(?<![a-zA-Z])" + re.escape(keyword_lower) + r"(?![a-zA-Z])")
    return bool(pattern.search(text_lower))


def _write_back(tailored: Dict[str, Any], address: str, new_text: str) -> None:
    parts = address.split(".")
    if parts[0] == "summary":
        # Summary sentences are audited read-only (no safe surgery on prose).
        return
    section, idx, _, bullet_idx = parts[0], int(parts[1]), parts[2], int(parts[3])
    try:
        tailored[section][idx]["bullets"][bullet_idx] = new_text
    except (KeyError, IndexError, TypeError):
        logger.warning("claim_guard: write-back failed for %s", address)
