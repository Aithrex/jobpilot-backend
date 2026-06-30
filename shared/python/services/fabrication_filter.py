"""
Fabrication filter — deterministic guard for the ONE truly binary fabrication:
a certification the candidate does not hold.

Design principle: deterministic code handles only claims that need NO judgment.
A certification is exactly that — you either passed the exam or you didn't; there
is no "core concepts transfer" defense for a certificate. Everything else
(adjacent languages like C# for a Python dev, complementary tools for an analyst
or marketer, role-baseline skills) is a JUDGMENT call about defensibility, and
that judgment belongs to the tailoring model (the prompt's Defensibility test)
and the match-gap reporter — NOT to a regex or a hand-maintained word list. An
earlier version of this module tried to classify languages/products/baseline
skills deterministically and wrongly blocked defensible additions (e.g. C#); that
logic has been removed.

Used by:
  * resume_tailor._ensure_jd_skills_coverage — never auto-inject a JD-listed
    certification the candidate doesn't hold.
  * IntegrityGuard — strip a certification the model placed in the skills section
    (the back door around the certifications field).

Pure functions, no LLM calls.
"""
import re
from typing import Any, Dict

from schemas.resume_schemas import build_resume_text
from utils.keyword_normalizer import get_all_forms, normalize_single

# A term is a certification claim if it self-identifies as one. Kept tight so
# generic phrasing doesn't over-trigger, but broad enough for the real patterns
# ("AWS Certified Solutions Architect", "Implementation Specialist
# Certification", "PMP Certification", "micro-certification").
_CERT_RE = re.compile(
    r"\b(?:certified|certification|certificate)\b"
    r"|\bimplementation specialist\b"
    r"|\bmicro[- ]?cert(?:ified|ification)?\b",
    re.IGNORECASE,
)


def is_certification_claim(term: str) -> bool:
    """True if the term reads as a named certification rather than a skill."""
    if not term:
        return False
    return bool(_CERT_RE.search(term))


def _word_bounded(text_lower: str, form: str) -> bool:
    """Word-boundary-aware match (mirrors keyword_gap_engine._text_contains)."""
    if not form:
        return False
    if len(form) <= 2:
        return form in text_lower
    return re.search(
        r"(?<![a-zA-Z])" + re.escape(form) + r"(?![a-zA-Z])", text_lower
    ) is not None


def is_grounded(term: str, original_resume: Dict[str, Any]) -> bool:
    """True if any alias of `term` already appears in the base resume text.

    The base resume is the source of truth for what the candidate actually has.
    """
    if not term:
        return False
    haystack = build_resume_text(original_resume).lower()
    return any(_word_bounded(haystack, f) for f in get_all_forms(term))


def _cert_in_base(term: str, original_resume: Dict[str, Any]) -> bool:
    """True if `term` matches a certification listed in the base resume.

    Checks the structured `certifications` list directly (build_resume_text does
    NOT include it) with a tolerant word-overlap match, then falls back to
    general resume-text grounding for certs mentioned elsewhere.
    """
    term_l = term.lower().strip()
    base_certs = original_resume.get("certifications") or []
    for c in base_certs:
        if not isinstance(c, str):
            continue
        cl = c.lower().strip()
        if cl == term_l or cl in term_l or term_l in cl:
            return True
    return is_grounded(term, original_resume)


def is_fabricated_certification(term: str, original_resume: Dict[str, Any]) -> bool:
    """True iff `term` is a certification claim NOT present in the base resume.

    This is the only fabrication we block deterministically — it requires no
    judgment. Defensibility of skills/tools is left to the model.
    """
    if not term or not term.strip():
        return False
    return is_certification_claim(term) and not _cert_in_base(term, original_resume)


# ---------------------------------------------------------------------------
# Non-skill detector (narrow safety net for the skills section)
# ---------------------------------------------------------------------------
#
# This catches ONE failure the model keeps making despite the prompt: pasting a
# JD's soft-trait / working-style PROSE into the skills section ("ownership and
# follow-through", "comfort with ambiguity", "collaborative working style").
# These are never skills in ANY profession — they are culture-fit sentences.
#
# Design constraints learned the hard way (three live cases where broad
# stripping caused net harm):
#   * Match ONLY this narrow, profession-agnostic class — soft-trait phrases.
#     Do NOT try to classify "is this a real skill" generally (the normalizer
#     doesn't cover specialized vocab; heuristics strip real competencies).
#   * A real, named technology is never a soft trait, so this can't eat
#     "PostgreSQL" / "Kafka" / "variance analysis".
#   * Require multi-word phrasing — single tokens like "Ownership" or
#     "Communication" are too ambiguous to strip safely and are left alone.
# Job-titles, bare domains, and vague catch-alls remain the PROMPT's job (too
# context-dependent to strip without harm); only the unambiguous soft-trait
# prose is removed here.

# Trait words that, in a multi-word phrase, mark it as culture-fit prose rather
# than a skill. Cross-profession.
_SOFT_TRAIT_WORDS = (
    "ownership", "follow-through", "follow through", "ambiguity", "collaborative",
    "collaboration", "working style", "work style", "self-starter", "self starter",
    "proactive", "proactively", "attention to detail", "detail-oriented",
    "team player", "fast-paced", "fast paced", "willingness", "passion", "passionate",
    "eager", "enthusiasm", "mindset", "go-getter", "can-do", "cultural fit",
    "culture fit", "bias for action", "growth mindset", "comfort with",
    "comfortable with ambiguity", "strong work ethic", "interpersonal",
)

# Connective/filler words that don't disqualify a phrase from being a trait.
_TRAIT_CONNECTIVES = {"and", "&", "with", "to", "for", "of", "the", "a", "in", "strong", "good"}


def is_nonskill_soft_trait(term: str) -> bool:
    """True iff `term` reads as JD soft-trait/working-style PROSE, not a skill.

    Narrow and conservative: only fires on MULTI-WORD phrases built around a
    recognized soft-trait word (so 'Ownership and Follow-Through', 'Comfort with
    Ambiguity', 'Collaborative Working Style' match; 'PostgreSQL', 'A/B testing',
    'variance analysis', and bare single words do not).
    """
    if not term or not term.strip():
        return False
    t = term.strip().lower()
    tokens = re.findall(r"[a-z][a-z\-]*", t)
    if len(tokens) < 2:
        return False  # single-token entries are too ambiguous to strip
    # Must contain a soft-trait marker as a phrase substring.
    if not any(marker in t for marker in _SOFT_TRAIT_WORDS):
        return False
    # Guard: if any token looks like a concrete tech/skill token (has digits,
    # camelCase, or is a known acronym-ish all-caps in the original term), don't
    # strip — these are not pure trait phrases.
    if re.search(r"[0-9/]", term) or re.search(r"[a-z][A-Z]", term):
        return False
    # Every NON-connective token must be part of trait vocabulary phrasing —
    # i.e. the phrase is dominated by trait/filler words, not a skill noun.
    # We accept it as a trait phrase as long as a trait marker is present and
    # the remaining tokens are connectives or trait words (no foreign skill noun
    # like 'sql', 'python', 'analysis').
    trait_token_set = set()
    for marker in _SOFT_TRAIT_WORDS:
        trait_token_set.update(marker.split())
    for tok in tokens:
        if tok in _TRAIT_CONNECTIVES:
            continue
        if tok in trait_token_set:
            continue
        # A token that's neither connective nor trait vocabulary => likely a real
        # skill noun rode along; be safe and DON'T strip.
        return False
    return True
