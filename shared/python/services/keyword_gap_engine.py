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
        req_matched, req_missing, _ = self._match_keywords(
            required, resume_text, resume_skills
        )

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
