"""
Impact Engine — deterministic bullet quality analysis for resumes.

Pure Python, no AI calls. Detects weak verbs, missing metrics,
and short/vague bullets. Produces an impact score and actionable suggestions.
"""
import re
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Weak opening phrases — bullets starting with these are flagged
WEAK_VERB_PHRASES = frozenset({
    "helped",
    "assisted",
    "worked on",
    "was responsible for",
    "participated in",
    "involved in",
    "contributed to",
    "supported",
    "tasked with",
    "responsible for",
    "assigned to",
    "in charge of",
    "handled",
    "dealt with",
    "took part in",
    "aided",
    "collaborated on",
})

# Regex for quantifiable metrics in bullets
METRIC_PATTERN = re.compile(r"\d+%|\$[\d,]+|\d+\+|\d+x|\b\d{2,}\b")

# Minimum meaningful bullet length
MIN_BULLET_LENGTH = 50


class ImpactEngine:
    """Deterministic bullet quality analyzer."""

    def analyze(self, tailored_resume: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze bullet quality across experience and projects.

        Returns:
            {
                "impact_score": int (0-100),
                "weak_bullets": [{section, entry, index, text, issues}],
                "suggestions": [str],
            }
        """
        weak_bullets: List[Dict[str, Any]] = []
        issue_counts = {"weak_verb": 0, "no_metrics": 0, "too_short": 0}
        total_bullets = 0

        # Analyze experience bullets
        for exp in tailored_resume.get("experience", []):
            entry_name = exp.get("company", "") or exp.get("title", "")
            for i, bullet in enumerate(exp.get("bullets", [])):
                total_bullets += 1
                issues = self._check_bullet(bullet)
                if issues:
                    weak_bullets.append({
                        "section": "experience",
                        "entry": entry_name,
                        "index": i,
                        "text": bullet,
                        "issues": issues,
                    })
                    for issue in issues:
                        issue_counts[issue] = issue_counts.get(issue, 0) + 1

        # Analyze project bullets
        for proj in tailored_resume.get("projects", []):
            entry_name = proj.get("name", "")
            for i, bullet in enumerate(proj.get("bullets", [])):
                total_bullets += 1
                issues = self._check_bullet(bullet)
                if issues:
                    weak_bullets.append({
                        "section": "projects",
                        "entry": entry_name,
                        "index": i,
                        "text": bullet,
                        "issues": issues,
                    })
                    for issue in issues:
                        issue_counts[issue] = issue_counts.get(issue, 0) + 1

        # Compute impact score
        impact_score = self._compute_score(issue_counts, total_bullets)

        # Generate actionable suggestions
        suggestions = self._generate_suggestions(issue_counts, total_bullets)

        return {
            "impact_score": impact_score,
            "weak_bullets": weak_bullets,
            "suggestions": suggestions,
        }

    def _check_bullet(self, bullet: str) -> List[str]:
        """Check a single bullet for quality issues."""
        issues: List[str] = []
        if not bullet:
            return ["too_short"]

        # Check for weak opening verbs/phrases
        bullet_lower = bullet.lower().strip()
        # Remove leading dash/bullet character
        bullet_lower = re.sub(r"^[-•*]\s*", "", bullet_lower)

        for phrase in WEAK_VERB_PHRASES:
            if bullet_lower.startswith(phrase):
                issues.append("weak_verb")
                break

        # Check for missing metrics
        if not METRIC_PATTERN.search(bullet):
            issues.append("no_metrics")

        # Check for too-short bullets
        if len(bullet.strip()) < MIN_BULLET_LENGTH:
            issues.append("too_short")

        return issues

    def _compute_score(self, issue_counts: dict, total_bullets: int) -> int:
        """Compute impact score from 100 downward based on issues found."""
        if total_bullets == 0:
            return 0

        score = 100
        score -= issue_counts.get("weak_verb", 0) * 5
        score -= issue_counts.get("no_metrics", 0) * 3
        score -= issue_counts.get("too_short", 0) * 4

        return max(0, min(100, score))

    def _generate_suggestions(
        self, issue_counts: dict, total_bullets: int
    ) -> List[str]:
        """Generate deduplicated actionable suggestions from issue analysis."""
        suggestions: List[str] = []

        if issue_counts.get("weak_verb", 0) > 0:
            n = issue_counts["weak_verb"]
            suggestions.append(
                f"Replace weak action verbs in {n} bullet(s) — use strong verbs "
                f"like 'Engineered', 'Architected', 'Deployed', 'Optimized', "
                f"'Spearheaded' instead of 'Helped', 'Assisted', 'Worked on'."
            )

        no_metrics = issue_counts.get("no_metrics", 0)
        if no_metrics > 0 and total_bullets > 0:
            pct = round((no_metrics / total_bullets) * 100)
            suggestions.append(
                f"Add quantifiable impact to {no_metrics} of {total_bullets} "
                f"bullets ({pct}%) — include percentages, dollar amounts, "
                f"user counts, or scale numbers."
            )

        if issue_counts.get("too_short", 0) > 0:
            n = issue_counts["too_short"]
            suggestions.append(
                f"Expand {n} brief bullet(s) — each bullet should include the "
                f"specific technology used, the action taken, and the measurable "
                f"outcome (aim for 100-200 characters)."
            )

        return suggestions
