"""
Keyword Gap Engine — deterministic keyword gap analysis between resume and JD.

Pure Python, no AI calls. Compares a parsed resume against a JD analysis
to identify matched, missing, and partially matched keywords.

Uses alias-aware matching via the universal keyword normalizer so that
"Postgres" in a JD matches "PostgreSQL" in a resume.
"""
import re
import logging
from typing import Any, Dict, List, Set, Tuple

from schemas.resume_schemas import build_resume_text, flatten_skills
from utils.keyword_normalizer import (
    normalize_single,
    normalize_keywords,
    get_all_forms,
)

logger = logging.getLogger(__name__)


class KeywordGapEngine:
    """Deterministic keyword gap analysis between a resume and a JD."""

    def analyze(
        self, parsed_resume: Dict[str, Any], jd_analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Analyze keyword coverage of a resume against a JD.

        Returns:
            {
                "matched_keywords": [],
                "missing_keywords": [],
                "partial_matches": [],
                "coverage_percentage": int,
                "required_coverage": int,
                "required_matched": [],
                "required_missing": [],
            }
        """
        resume_text = build_resume_text(parsed_resume).lower()
        resume_skills = set(s.lower() for s in flatten_skills(parsed_resume))

        # Normalize JD keyword lists
        required = normalize_keywords(jd_analysis.get("required_skills", []))
        preferred = normalize_keywords(jd_analysis.get("preferred_skills", []))
        keywords = normalize_keywords(jd_analysis.get("keywords", []))

        # Deduplicated combined list (required first for priority)
        all_keywords = list(dict.fromkeys(required + preferred + keywords))

        # Analyze all keywords
        matched, missing, partial = self._match_keywords(
            all_keywords, resume_text, resume_skills
        )

        # Separate analysis for required skills only
        req_matched, req_missing, req_partial = self._match_keywords(
            required, resume_text, resume_skills
        )

        # Implication promotion: a JD skill the resume didn't literally name but
        # which an OWNED skill provably implies (e.g. React -> HTML/CSS/DOM) is
        # covered, not a gap. The candidate can defend it because doing the
        # parent skill necessarily involves it. Promoted skills are also added to
        # `partial` so downstream (prompt/audit) know they're implied, not
        # literal — the tailor should surface them in context, not assume prose.
        owned = self._owned_skills(matched, resume_skills)
        matched, missing, promoted = self._promote_implied(matched, missing, owned)
        req_matched, req_missing, req_promoted = self._promote_implied(
            req_matched, req_missing, owned
        )
        for p in promoted:
            if p not in partial:
                partial.append(p)

        total = len(all_keywords)
        coverage = round((len(matched) / total) * 100) if total > 0 else 100

        req_total = len(required)
        req_coverage = round((len(req_matched) / req_total) * 100) if req_total > 0 else 100

        return {
            "matched_keywords": matched,
            "missing_keywords": missing,
            "partial_matches": partial,
            "coverage_percentage": coverage,
            "required_coverage": req_coverage,
            "required_matched": req_matched,
            "required_missing": req_missing,
        }

    def _match_keywords(
        self,
        keywords: List[str],
        resume_text: str,
        resume_skills: Set[str],
    ) -> Tuple[List[str], List[str], List[str]]:
        """Match keywords against resume text using alias-aware search.

        Returns (matched, missing, partial_matches).
        """
        matched: List[str] = []
        missing: List[str] = []
        partial: List[str] = []

        for kw in keywords:
            canonical = normalize_single(kw)
            all_forms = get_all_forms(kw)

            found = False
            found_via_alias = False

            # Check each known form of this keyword
            for form in all_forms:
                if self._text_contains(resume_text, form):
                    found = True
                    if form != canonical.lower():
                        found_via_alias = True
                    break

            # Also check in the skills set (exact match after normalization)
            if not found and canonical.lower() in resume_skills:
                found = True

            if found:
                matched.append(canonical)
                if found_via_alias:
                    partial.append(canonical)
            else:
                missing.append(canonical)

        return matched, missing, partial

    @staticmethod
    def _owned_skills(matched: List[str], resume_skills: Set[str]) -> Set[str]:
        """The set of skills the candidate genuinely HAS, for implication lookup.

        Union of (a) every JD keyword the resume literally matched and (b) the
        resume's own skills section. Parenthetical umbrellas ("AWS (EC2, S3)")
        contribute both the umbrella and each inner token so an owned "EKS"
        inside "AWS (EKS)" can still imply Kubernetes.
        """
        owned: Set[str] = set(matched)
        for s in resume_skills:
            owned.add(s)
            # Split "aws (ec2, s3, eks)" into aws + ec2 + s3 + eks.
            if "(" in s:
                head, _, rest = s.partition("(")
                if head.strip():
                    owned.add(head.strip())
                for tok in rest.rstrip(")").split(","):
                    if tok.strip():
                        owned.add(tok.strip())
        return owned

    @staticmethod
    def _promote_implied(
        matched: List[str], missing: List[str], owned: Set[str]
    ) -> Tuple[List[str], List[str], List[str]]:
        """Promote missing keywords an owned skill provably implies to matched.

        Returns (matched, missing, promoted). Deterministic, conservative — only
        the SOLID implication tier promotes, and forbidden reverse edges never do
        (see utils.skill_implications).
        """
        from utils.skill_implications import is_implied

        still_missing: List[str] = []
        promoted: List[str] = []
        for kw in missing:
            if is_implied(kw, owned):
                matched.append(kw)
                promoted.append(kw)
            else:
                still_missing.append(kw)
        return matched, still_missing, promoted

    @staticmethod
    def _text_contains(text_lower: str, keyword_lower: str) -> bool:
        """Word-boundary-aware keyword search in lowercased text."""
        if not keyword_lower:
            return False
        if len(keyword_lower) <= 2:
            return keyword_lower in text_lower
        pattern = re.compile(
            r"(?<![a-zA-Z])" + re.escape(keyword_lower) + r"(?![a-zA-Z])"
        )
        return bool(pattern.search(text_lower))
