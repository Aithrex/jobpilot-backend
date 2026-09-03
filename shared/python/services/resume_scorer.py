"""
Resume Scorer — honest, deterministic ATS scoring (schema_version 2).

The overall score is 100% deterministic and reproducible:

    hard_skill_match      30%   alias-aware JD keyword coverage, placement-aware
    title_alignment       15%   target title mirrored in summary/recent role
    quantified_impact     15%   metric-bearing bullets (density + diversity)
    ats_parseability      15%   REAL pypdf round-trip score — never hardcoded
    section_completeness  10%   summary/skills/experience/education present
    recency_relevance      8%   reverse-chron order + JD terms in recent role
    readability            7%   bullet length/openers/verb diversity/dates

An LLM pass (FLASH) contributes only advisory fields — experience_relevance,
suggestions, strengths — and NEVER moves the overall. If it fails, those
advisory fields are marked unavailable rather than inventing a number.

What v2 deliberately removed: the six per-vendor "scanner scores" (they were
LLM guesses coaxed into 85-95 — real ATSes don't emit scores) and the
ai_screener simulation. Per-vendor output is now `vendor_compat`: real
pass/warn/fail parse checklists from `ats_vendor_checks`.

Design rules:
- Render failure is LOUD: no PDF → parseability 0, overall capped at 40.
- Anti-stuffing: a skill listed with no supporting evidence earns 0 credit.
  Hammering a term 4+ times, an unevidenced skills dump, and over-long
  skill rows are penalized. Honest target band is 75-80.
"""
import logging
import re
from typing import Any, Dict, List, Tuple

from schemas.resume_schemas import (
    ATS_SCORES_SCHEMA,
    validate_and_coerce,
    build_resume_text,
)
from utils.keyword_normalizer import (
    normalize_single,
    normalize_keywords,
    get_all_forms,
    get_acronym_pair,
)
from services.ats_vendor_checks import (
    build_vendor_compat,
    dates_format_consistent,
    is_reverse_chronological,
)

logger = logging.getLogger(__name__)


# Keywords the suggestions engine must NOT recommend the candidate "add."
# JD logistics terms describe the role's working arrangement, not skills.
# Suggesting a candidate add "Contract W2" to their resume is bad advice —
# recruiters use those terms to filter the JD, not to evaluate candidates.
_LOGISTICS_KEYWORDS = frozenset({
    "contract w2", "contract", "hybrid", "remote", "on-site", "onsite",
    "full-time", "part-time", "fulltime", "parttime", "w2", "1099",
    "us citizen", "green card", "h1b", "h-1b", "visa sponsorship",
    "no sponsorship", "work authorization", "available for", "open to",
})

# Compliance frameworks require demonstrated domain experience. Suggesting
# "Add FedRAMP to your skills" to a candidate with no federal-cloud work
# is encouraging fabrication. These belong on a resume only when bullets
# already show work in the matching domain.
_COMPLIANCE_FRAMEWORKS = frozenset({
    # Tech / govcloud security & privacy
    "fedramp", "soc 2", "soc2", "hipaa", "pci-dss", "pci dss", "pci",
    "sox", "iso 27001", "iso27001", "iso", "nist 800-53", "nist", "gdpr",
    "ccpa", "ato", "fisma", "hitrust", "cmmc",
    # Non-tech credentials/licenses (Phase 3C de-SWE) — like compliance
    # frameworks, these require real domain exposure and must never be
    # suggested as a free "add this skill".
    "cpa", "cfa", "series 7", "series 63", "series 65", "series 66",
    "rn license", "registered nurse license", "bls certification",
    "acls certification", "pals certification", "pmp", "csm",
    "six sigma black belt", "six sigma green belt", "bar admission",
    "j.d.", "jd license", "bar exam", "cma", "cia", " chfc", "cpcu",
})


def _classify_missing_keywords(missing: List[str]) -> Dict[str, List[str]]:
    """Partition missing JD keywords into skills / logistics / compliance.

    Only the `skills` bucket is safe to surface to the suggestions model.
    `logistics` describes the JD's working arrangement (Contract W2, Hybrid);
    `compliance` requires domain evidence (FedRAMP, HIPAA, SOX) and should
    never be suggested as a free addition.
    """
    skills: List[str] = []
    logistics: List[str] = []
    compliance: List[str] = []
    for kw in missing or []:
        if not isinstance(kw, str):
            continue
        low = kw.strip().lower()
        if not low:
            continue
        if low in _LOGISTICS_KEYWORDS:
            logistics.append(kw)
        elif low in _COMPLIANCE_FRAMEWORKS:
            compliance.append(kw)
        else:
            skills.append(kw)
    return {"skills": skills, "logistics": logistics, "compliance": compliance}


class ResumeScorer:
    """Deterministic ATS scorer with an advisory (non-scoring) LLM review."""

    # Tokens stripped from titles before comparison so honest mirroring
    # scores full marks and the scorer never incentivizes fabricated
    # seniority ("Senior Software Engineer" JD vs "Software Engineer II"
    # history → same core tokens).
    _SENIORITY_TOKENS = frozenset({
        "senior", "sr", "staff", "principal", "lead", "junior", "jr",
        "associate", "intern", "entry", "mid", "level",
        "i", "ii", "iii", "iv", "v", "1", "2", "3",
    })
    _TITLE_STOPWORDS = frozenset({"of", "the", "and", "a", "an", "&", "-"})
    # Cross-profession title folding so honest title mirroring scores full marks
    # regardless of the exact word a JD/resume uses (Phase 3C de-SWE).
    _ROLE_EQUIV = {
        # software
        "developer": "engineer", "programmer": "engineer", "swe": "engineer",
        # healthcare
        "rn": "nurse", "r.n.": "nurse", "registered": "nurse",
        # finance
        "cpa": "accountant", "accounting": "accountant",
        # sales
        "ae": "sales", "rep": "sales", "representative": "sales",
        # generic
        "mgr": "manager", "coordinator": "specialist", "analyst": "analyst",
    }

    def score(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        pdf_bytes: bytes | None = None,
        render_error: str | None = None,
        page_outcome: str | None = None,
    ) -> Dict[str, Any]:
        """Run the full scoring pipeline. Returns a validated v2 scores dict.

        `pdf_bytes` is the rendered PDF; it is round-tripped through pypdf
        (the same library real ATSes use) for the parseability dimension.
        `render_error` carries the reason when the caller failed to render —
        a missing PDF is a LOUD failure (parseability 0, overall ≤ 40),
        never a silent skip: a resume that can't render can't be parsed by
        any ATS, so every other metric is moot.
        """
        from services.term_registry import sanitize_jd_analysis
        jd_analysis = sanitize_jd_analysis(dict(jd_analysis or {}))

        det = self._deterministic_scores(tailored, jd_analysis)

        pdf_check = None
        if pdf_bytes:
            try:
                from services.pdf_ats_check import check_pdf_parseability
                pdf_check = check_pdf_parseability(
                    pdf_bytes, tailored, jd_analysis, outcome=page_outcome,
                )
            except Exception as e:
                logger.warning("PDF parseability check failed: %s", e)
                render_error = render_error or f"Parseability check failed: {e}"
        elif not render_error:
            render_error = "Resume PDF was not rendered"

        ai = self._ai_review(tailored, jd_analysis, det)
        vendor_compat = build_vendor_compat(pdf_check, tailored)
        combined = self._combine(det, ai, pdf_check, vendor_compat, render_error)

        # Loosely-shaped extras attached AFTER validation (bools/dicts the
        # lightweight schema DSL doesn't express).
        combined["missing_keywords_classified"] = _classify_missing_keywords(
            det.get("missing_keywords", [])
        )
        combined["ai_review_available"] = bool(ai.get("_available", True))
        combined["rubric_detail"] = {
            "hard_skill_match": det.get("hard_skill_detail", {}),
            "title_alignment": det.get("title_detail", {}),
        }
        combined["unevidenced_keywords"] = list(
            (det.get("hard_skill_detail") or {}).get("unevidenced_keywords") or []
        )
        combined["stuffing_warnings"] = self._stuffing_warnings(tailored)
        combined["jd_context"] = (jd_analysis or {}).get("jd_context") or {}
        if (det.get("hard_skill_detail") or {}).get("unscored"):
            combined["hard_skill_unscored"] = True
        if ai.get("experience_relevance") is None:
            combined["experience_relevance"] = None
        kg = (det.get("hard_skill_detail") or {}).get("keyword_gap")
        if kg:
            combined["keyword_gap"] = kg
        if pdf_check:
            combined["pdf_parseable_score"] = pdf_check["pdf_parseable_score"]
            combined["pdf_parseable_checks"] = pdf_check["checks"]
            combined["pdf_parseable_warnings"] = pdf_check["warnings"]
        else:
            combined["pdf_parseable_score"] = 0
            combined["pdf_parseable_checks"] = {}
            combined["pdf_parseable_warnings"] = [
                "The resume PDF failed to render or parse — no ATS can read "
                f"it in this state. ({render_error})"
            ]

        return combined

    # ------------------------------------------------------------------
    # Deterministic scoring (pure Python, no AI)
    # ------------------------------------------------------------------

    def _deterministic_scores(
        self, tailored: Dict[str, Any], jd_analysis: Dict[str, Any]
    ) -> dict:
        """Compute every rubric dimension except parseability."""
        hard_score, found_kw, missing_kw, hard_detail = self._hard_skill_score(
            tailored, jd_analysis
        )
        title_score, title_detail = self._title_alignment_score(tailored, jd_analysis)

        # Impact engine analysis (deterministic bullet quality) — blended
        # into quantified_impact so its verb/metric heuristics stay
        # load-bearing without being a separate opaque dimension.
        from services.impact_engine import ImpactEngine
        impact_analysis = ImpactEngine().analyze(tailored)
        metric_density = self._quantifiable_impact_score(tailored)
        quantified = round(
            0.7 * metric_density + 0.3 * int(impact_analysis.get("impact_score", 0))
        )

        return {
            "hard_skill_match": hard_score,
            "title_alignment": title_score,
            "quantified_impact": max(0, min(100, quantified)),
            "section_completeness": self._section_completeness_score(tailored),
            "recency_relevance": self._recency_relevance_score(tailored, jd_analysis),
            "readability": self._readability_score(tailored),
            "missing_keywords": missing_kw,
            "found_keywords": found_kw,
            "hard_skill_detail": hard_detail,
            "title_detail": title_detail,
            "impact_suggestions": impact_analysis.get("suggestions", []),
        }

    # -- hard_skill_match (30%) ----------------------------------------

    def _build_context_text(self, tailored: Dict[str, Any]) -> str:
        """Summary + experience/project bullets — the zones where a keyword
        counts as USED, not just listed. The skills list is deliberately
        excluded: recruiters discount bare skill dumps, and crediting them
        equally would reward keyword-stuffing the skills section.
        """
        parts: List[str] = [tailored.get("summary") or ""]
        for exp in tailored.get("experience") or []:
            parts.extend(b for b in (exp.get("bullets") or []) if isinstance(b, str))
        for proj in tailored.get("projects") or []:
            parts.extend(b for b in (proj.get("bullets") or []) if isinstance(b, str))
        return "\n".join(p for p in parts if p).lower()

    def _hard_skill_score(
        self, tailored: Dict[str, Any], jd_analysis: Dict[str, Any]
    ) -> Tuple[int, List[str], List[str], Dict[str, Any]]:
        """Alias-aware, placement-aware JD keyword coverage.

        Returns (score, found, missing, detail). Required skills weigh 70%,
        preferred+other keywords 30%. A term that appears only in the skills
        list, with no supporting evidence elsewhere, earns 0. Implied SOLID
        coverage (from KeywordGapEngine) is credited so the scorer and gap
        engine agree on what is matched.
        """
        from services.keyword_gap_engine import KeywordGapEngine
        from services.term_registry import (
            count_occurrences,
            evidence_weight,
            term_in_structured,
            text_contains,
        )

        required = normalize_keywords(jd_analysis.get("required_skills", []))
        req_lower = {r.lower() for r in required}
        other = [
            k for k in normalize_keywords(
                (jd_analysis.get("preferred_skills") or [])
                + (jd_analysis.get("keywords") or [])
            )
            if k.lower() not in req_lower
        ]

        if not required and not other:
            return 0, [], [], {
                "note": "no JD keywords found",
                "unscored": True,
                "required_coverage": 0,
                "other_coverage": 0,
                "context_fraction": 0,
                "acronym_pairs_satisfied": 0,
                "stuffed_keywords": 0,
                "unevidenced_listed": 0,
                "unevidenced_keywords": [],
            }

        # One matcher: the gap engine (aliases, parentheticals, umbrellas,
        # SOLID implications). The scorer only applies the evidence gradient.
        gap = KeywordGapEngine().analyze(tailored, jd_analysis)
        matched_set = {m.lower() for m in (gap.get("matched_keywords") or [])}
        resume_text = build_resume_text(tailored).lower()

        found: List[str] = []
        missing: List[str] = []
        unevidenced_keywords: List[str] = []
        req_weight = other_weight = in_context = pair_hits = stuff_hits = 0.0

        for kw, is_req in [(k, True) for k in required] + [(k, False) for k in other]:
            canonical = normalize_single(kw)
            matched = canonical.lower() in matched_set
            if not matched:
                missing.append(canonical)
                continue
            weight = evidence_weight(kw, tailored)
            if weight <= 0 and not term_in_structured(kw, tailored, include_skills=True):
                # Implied by an owned parent skill — treat as evidenced coverage
                # so the scorer and gap engine report the same matched set.
                weight = 0.6
            if weight <= 0:
                unevidenced_keywords.append(canonical)
                continue
            found.append(canonical)
            if is_req:
                req_weight += weight
            else:
                other_weight += weight
            if weight >= 1.0:
                in_context += 1
            pair = get_acronym_pair(kw)
            if pair and all(text_contains(resume_text, p) for p in pair):
                pair_hits += 1
            if count_occurrences(resume_text, kw) > 3:
                stuff_hits += 1

        req_cov = (req_weight / len(required)) if required else 1.0
        other_cov = (other_weight / len(other)) if other else 1.0
        base = 100 * (0.70 * req_cov + 0.30 * other_cov)

        context_frac = in_context / max(1, len(found))
        score = 0.75 * base + 25 * context_frac
        score += min(pair_hits * 2, 6)
        score -= min(stuff_hits * 3, 15)
        score -= self._anti_stuff_penalty(tailored, found)

        detail = {
            "required_coverage": round(req_cov * 100),
            "other_coverage": round(other_cov * 100),
            "context_fraction": round(context_frac * 100),
            "acronym_pairs_satisfied": pair_hits,
            "stuffed_keywords": int(stuff_hits),
            "unevidenced_listed": len(unevidenced_keywords),
            "unevidenced_keywords": unevidenced_keywords,
            "keyword_gap": {
                "matched_keywords": list(gap.get("matched_keywords") or []),
                "missing_keywords": list(gap.get("missing_keywords") or []),
                "partial_matches": list(gap.get("partial_matches") or []),
                "required_matched": list(gap.get("required_matched") or []),
                "required_missing": list(gap.get("required_missing") or []),
                "coverage_percentage": gap.get("coverage_percentage", 0),
                "required_coverage": gap.get("required_coverage", 0),
            },
        }
        return int(max(0, min(100, round(score)))), found, missing, detail

    @staticmethod
    def _anti_stuff_penalty(tailored: Dict[str, Any], found: List[str]) -> int:
        """Visible-to-user stuffing constraints. Silent numeric penalty only."""
        from schemas.resume_schemas import flatten_skills
        from services.term_registry import evidence_weight

        skills = flatten_skills(tailored) or []
        if not skills:
            return 0
        evidenced = sum(1 for s in skills if evidence_weight(s, tailored) >= 0.6)
        ratio = evidenced / max(1, len(skills))
        penalty = 0
        if ratio < 0.35:
            penalty += 12
        elif ratio < 0.50:
            penalty += 6
        from services.resume_parser import ResumeParser
        from services.term_registry import skill_term_ceiling
        years = ResumeParser._estimate_experience_years(tailored)
        ceiling = skill_term_ceiling(
            years, has_roles=bool(tailored.get("experience")),
        )
        if len(skills) > ceiling:
            penalty += 8
        for items in (tailored.get("skills") or {}).values() if isinstance(
            tailored.get("skills"), dict
        ) else []:
            if isinstance(items, list) and len(items) > 12:
                penalty += 3
                break
        return min(penalty, 20)

    @staticmethod
    def _stuffing_warnings(tailored: Dict[str, Any]) -> List[str]:
        """User-visible stuffing signals — not a silent penalty."""
        from schemas.resume_schemas import flatten_skills
        from services.term_registry import count_occurrences, evidence_weight, text_contains

        warnings: List[str] = []
        skills = [s for s in (flatten_skills(tailored) or []) if str(s).strip()]
        if skills:
            unevidenced = [s for s in skills if evidence_weight(s, tailored) < 0.6]
            if unevidenced:
                warnings.append(
                    f"{len(unevidenced)} of your {len(skills)} listed skills do not "
                    "appear anywhere else in the resume. Screeners notice this."
                )
            from services.resume_parser import ResumeParser
            from services.term_registry import skill_term_ceiling
            years = ResumeParser._estimate_experience_years(tailored)
            ceiling = skill_term_ceiling(
                years, has_roles=bool(tailored.get("experience")),
            )
            if len(skills) > ceiling:
                warnings.append(
                    f"The skills section lists {len(skills)} terms "
                    f"(cap is {ceiling} for ~{years or 0} years of experience). "
                    "Long inventories read as padding."
                )
            skills_obj = tailored.get("skills")
            if isinstance(skills_obj, dict):
                for heading, items in skills_obj.items():
                    if isinstance(items, list) and len(items) > 12:
                        warnings.append(
                            f'"{heading}" has {len(items)} items. Keep each '
                            "row to 12 or fewer so it stays scannable."
                        )
                        break

        blob = build_resume_text(tailored).lower()
        repeated = [
            s for s in skills
            if count_occurrences(blob, s) > 3
        ]
        if repeated:
            warnings.append(
                f"{repeated[0]} appears more than 3 times. Repeating a "
                "keyword to raise density is visible to a reader."
            )

        dense = 0
        for exp in tailored.get("experience") or []:
            for b in exp.get("bullets") or []:
                if not isinstance(b, str):
                    continue
                hits = sum(1 for s in skills if text_contains(b.lower(), s))
                if hits > 4:
                    dense += 1
        if dense:
            warnings.append(
                f"{dense} bullet(s) name more than 4 technologies. "
                "Beyond that the line becomes a list, not an accomplishment."
            )
        return warnings[:5]

    # -- title_alignment (15%) -------------------------------------------

    @classmethod
    def _core_title_tokens(cls, title: str) -> set:
        """Role-defining tokens of a job title: seniority + stopwords
        stripped, developer/programmer/swe folded into 'engineer'."""
        tokens = re.findall(r"[a-z0-9#+.]+", (title or "").lower())
        return {
            cls._ROLE_EQUIV.get(t, t)
            for t in tokens
            if t not in cls._SENIORITY_TOKENS and t not in cls._TITLE_STOPWORDS
        }

    def _title_alignment_score(
        self, tailored: Dict[str, Any], jd_analysis: Dict[str, Any]
    ) -> Tuple[int, Dict[str, Any]]:
        """How visibly the target job title is mirrored — the single
        strongest recruiter signal (title-matching resumes get ~10x the
        interviews). Measured against the summary and role titles; the
        floor is 10, not 0, so career changers with a bridge summary
        aren't nuked.
        """
        core = self._core_title_tokens(jd_analysis.get("job_title", ""))
        if not core:
            return 100, {"note": "no JD title extracted"}

        summary_low = (tailored.get("summary") or "").lower()
        titles = [e.get("title", "") for e in (tailored.get("experience") or [])]

        def _frac(token_set: set) -> float:
            return len(core & token_set) / len(core)

        summary_cov = sum(
            1 for t in core if self._text_contains(summary_low, t)
        ) / len(core)
        # Fold role equivalents when scanning the summary too, so a
        # "Full Stack Developer" summary matches an "engineer" core token.
        if summary_cov < 1.0:
            summary_cov = max(summary_cov, _frac(self._core_title_tokens(summary_low)))

        recent_cov = _frac(self._core_title_tokens(titles[0])) if titles else 0.0
        any_cov = max(
            (_frac(self._core_title_tokens(t)) for t in titles), default=0.0
        )

        if summary_cov == 1.0 and max(recent_cov, any_cov) >= 0.5:
            score = 100
        elif summary_cov == 1.0:
            score = 88
        elif recent_cov == 1.0:
            score = 80
        elif summary_cov >= 0.5 and max(recent_cov, any_cov) >= 0.5:
            score = 70
        elif max(summary_cov, recent_cov, any_cov) >= 0.5:
            score = 55
        elif max(summary_cov, recent_cov, any_cov) > 0:
            score = 35
        else:
            score = 10

        return score, {
            "summary_coverage": round(summary_cov * 100),
            "recent_title_coverage": round(recent_cov * 100),
            "jd_core_tokens": sorted(core),
        }

    # -- recency_relevance (8%) --------------------------------------------

    def _recency_relevance_score(
        self, tailored: Dict[str, Any], jd_analysis: Dict[str, Any]
    ) -> int:
        """Recruiters anchor on the most recent role: reverse-chron order
        (40), top JD terms present in the newest role (40, proportional),
        and a strong first bullet — keyword or metric — in that role (20).
        """
        exps = tailored.get("experience") or []
        if not exps:
            return 0
        score = 0

        if is_reverse_chronological(tailored):
            score += 40

        top_terms = (
            normalize_keywords(jd_analysis.get("required_skills", []))
            + normalize_keywords(jd_analysis.get("keywords", []))
        )[:8]
        recent = exps[0]
        recent_text = (
            (recent.get("title") or "")
            + " "
            + " ".join(b for b in (recent.get("bullets") or []) if isinstance(b, str))
        ).lower()
        if top_terms:
            hits = sum(
                1 for t in top_terms
                if any(self._text_contains(recent_text, f) for f in get_all_forms(t))
            )
            score += round(40 * hits / len(top_terms))
        else:
            score += 40

        first_bullet = next(
            (b for b in (recent.get("bullets") or []) if isinstance(b, str)), ""
        )
        has_metric = any(
            p.search(first_bullet) for _, p in self._METRIC_TYPE_PATTERNS
        )
        has_kw = any(
            any(self._text_contains(first_bullet.lower(), f) for f in get_all_forms(t))
            for t in top_terms
        )
        if has_metric or has_kw:
            score += 20

        return min(score, 100)

    # -- readability (7%) ---------------------------------------------------

    def _readability_score(self, tailored: Dict[str, Any]) -> int:
        """Scannability heuristics for the 7-second human pass:
        bullet length band 60-230 chars (30), strong openers (25), opening-
        verb diversity (15), summary 1-4 sentences ≤450 chars (15), one
        consistent date format (15).
        """
        from services.impact_engine import WEAK_VERB_PHRASES

        bullets: List[str] = []
        for exp in tailored.get("experience") or []:
            bullets.extend(b for b in (exp.get("bullets") or []) if isinstance(b, str))
        for proj in tailored.get("projects") or []:
            bullets.extend(b for b in (proj.get("bullets") or []) if isinstance(b, str))

        score = 0.0
        if bullets:
            in_band = sum(1 for b in bullets if 60 <= len(b.strip()) <= 230)
            score += 30 * in_band / len(bullets)

            def _strong_opener(b: str) -> bool:
                stripped = b.strip()
                if not stripped or not stripped[0].isalpha() or not stripped[0].isupper():
                    return False
                low = stripped.lower()
                if low.startswith("i "):
                    return False
                return not any(low.startswith(w) for w in WEAK_VERB_PHRASES)

            strong = sum(1 for b in bullets if _strong_opener(b))
            score += 25 * strong / len(bullets)

            first_words: Dict[str, int] = {}
            for b in bullets:
                words = b.strip().split()
                if words:
                    key = words[0].lower()
                    first_words[key] = first_words.get(key, 0) + 1
            overuse = sum(max(0, n - 2) for n in first_words.values())
            score += max(0, 15 - 5 * overuse)

        summary = (tailored.get("summary") or "").strip()
        if summary:
            sentences = [s for s in re.split(r"[.!?]+", summary) if s.strip()]
            length_ok = len(summary) <= 450
            count_ok = 1 <= len(sentences) <= 4
            if length_ok and count_ok:
                score += 15
            elif length_ok or count_ok:
                score += 7

        if dates_format_consistent(tailored):
            score += 15

        return int(max(0, min(100, round(score))))

    # -- section completeness (10%) -----------------------------------------

    def _section_completeness_score(self, tailored: Dict[str, Any]) -> int:
        """25 points each for non-empty: summary, skills, experience, education."""
        score = 0
        if tailored.get("summary"):
            score += 25
        if tailored.get("skills"):
            score += 25
        if tailored.get("experience"):
            score += 25
        if tailored.get("education"):
            score += 25
        return score

    # -- quantified impact (15%, blended with ImpactEngine) ------------------

    # Patterns classifying each metric *type*. A bullet that hits multiple
    # types still counts as one metric bullet, but contributes multiple
    # types to the diversity set. Order: more-specific patterns first so a
    # '420ms' isn't double-counted as both 'duration' and 'count'.
    _METRIC_TYPE_PATTERNS = (
        ("percentage", re.compile(r"\d+(?:\.\d+)?%")),
        ("monetary",   re.compile(r"\$[\d,]+(?:\.\d+)?[KMB]?")),
        ("range",      re.compile(r"\b\d+\s*(?:to|→|-)\s*\d+\b", re.IGNORECASE)),
        ("duration",   re.compile(
            r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|second|seconds|min|hour|hours|day|days|week|weeks|month|months|year|years)\b",
            re.IGNORECASE,
        )),
        ("multiplier", re.compile(r"\b\d+x\b", re.IGNORECASE)),
        ("count",      re.compile(r"\b\d{2,}(?:,\d{3})*(?:[KMB])?\b", re.IGNORECASE)),
    )

    # Patterns that signal fake-precision hedging. Each occurrence subtracts
    # 2 from the score, capped at -10 total. These mirror the IntegrityGuard
    # substitutions — anything that slipped past the guard still gets penalized.
    _AI_TELL_PENALTY_PATTERNS = (
        re.compile(r"\b\d+(?:\.\d+)?[KM]\+", re.IGNORECASE),      # 120K+, 2M+
        re.compile(r"\d+(?:\.\d+)?%\+"),                          # 85%+, 99.9%+
        re.compile(r"~\d"),                                        # ~5000
        re.compile(r"\bsub-\d", re.IGNORECASE),                   # sub-100ms
        re.compile(r"\bsub-(?:second|millisecond)\b", re.IGNORECASE),
        re.compile(r"\bnear-(?:zero|perfect|100%)\b", re.IGNORECASE),
    )

    def _quantifiable_impact_score(self, tailored: Dict[str, Any]) -> int:
        """Metric density scored realistically, still pure-Python:
          1. Count bullets with at least one metric. Base = min(count * 8, 80).
             Cap at 80 so raw count alone never tops the score.
          2. Diversity bonus: +5 per distinct metric *type* beyond the first,
             capped at +20.
          3. AI-tell penalty: -2 per occurrence of `K+`, `M+`, `%+`, `~N`,
             `sub-N`, `near-X`, capped at -10.
          4. Clamp to [0, 100].
        """
        count = 0
        types_seen: set = set()

        all_bullets: List[str] = []
        for exp in tailored.get("experience", []):
            all_bullets.extend(exp.get("bullets", []) or [])
        for proj in tailored.get("projects", []):
            all_bullets.extend(proj.get("bullets", []) or [])

        for bullet in all_bullets:
            if not isinstance(bullet, str):
                continue
            bullet_types: set = set()
            for type_name, pattern in self._METRIC_TYPE_PATTERNS:
                if pattern.search(bullet):
                    bullet_types.add(type_name)
            if bullet_types:
                count += 1
                types_seen.update(bullet_types)

        base = min(count * 8, 80)
        diversity_bonus = min(max(len(types_seen) - 1, 0) * 5, 20)

        ai_tell_hits = 0
        for bullet in all_bullets:
            if not isinstance(bullet, str):
                continue
            for pattern in self._AI_TELL_PENALTY_PATTERNS:
                ai_tell_hits += len(pattern.findall(bullet))
        ai_tell_penalty = min(ai_tell_hits * 2, 10)

        score = base + diversity_bonus - ai_tell_penalty
        return max(0, min(score, 100))

    # ------------------------------------------------------------------
    # Text matching helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _text_contains(text_lower: str, keyword_lower: str) -> bool:
        """Word-boundary-aware keyword search. Delegates to term_registry."""
        from services.term_registry import text_contains
        return text_contains(text_lower, keyword_lower)

    @staticmethod
    def _count_occurrences(text_lower: str, keyword_lower: str) -> int:
        from services.term_registry import count_occurrences
        return count_occurrences(text_lower, keyword_lower)

    # ------------------------------------------------------------------
    # Advisory LLM review (Claude FLASH on Bedrock — never moves `overall`)
    # ------------------------------------------------------------------

    def _ai_review(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        det: dict,
    ) -> dict:
        """Small FLASH call for what can't be computed deterministically:
        experience_relevance (career-trajectory fit) + suggestions +
        strengths. Failure degrades to a deterministic proxy — the overall
        score is unaffected by design.
        """
        try:
            return self._ai_review_call(tailored, jd_analysis, det)
        except Exception as e:
            logger.warning("AI review failed; experience_relevance unavailable: %s", e)
            classified = _classify_missing_keywords(det.get("missing_keywords", []))
            suggestions = list(det.get("impact_suggestions", []))[:5]
            safe_missing = classified["skills"][:3]
            if safe_missing:
                suggestions.append(
                    "If these JD skills show up in a bullet you already have, "
                    f"name them there: {', '.join(safe_missing)}"
                )
            strengths: List[str] = []
            if det["hard_skill_match"] >= 75:
                strengths.append("Strong coverage of the JD's required skills")
            if det["title_alignment"] >= 80:
                strengths.append("Target role title is clearly mirrored")
            if det["quantified_impact"] >= 60:
                strengths.append("Bullets carry concrete, quantified outcomes")
            return {
                "experience_relevance": None,
                "suggestions": suggestions or ["No suggestions available"],
                "strengths": strengths or ["Resume submitted for review"],
                "_available": False,
            }

    def _ai_review_call(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        det: dict,
    ) -> dict:
        import json
        from services.claude_client import claude_json, CLAUDE_FLASH

        tailored_json = json.dumps(tailored, indent=2)[:4000]
        jd_json = json.dumps(jd_analysis, indent=2)[:2000]

        # Domain-aware recruiter persona (Phase 3C de-SWE): a finance/nursing/
        # marketing résumé should be reviewed by a recruiter framed for that
        # field, not always "technical". Fails safe to a generic recruiter.
        try:
            from services.domain_classifier import classify_domain
            _domain = classify_domain(jd_analysis, tailored)
        except Exception:
            _domain = "general"
        _persona = {
            # tech
            "software": "expert technical recruiter",
            "data_science": "expert data-science recruiter",
            "data_engineering": "expert data-engineering recruiter",
            "data_analytics": "expert data-analytics recruiter",
            "devops": "expert DevOps/platform recruiter",
            "cybersecurity": "expert cybersecurity recruiter",
            "it_support": "expert IT/infrastructure recruiter",
            # business
            "finance": "expert finance recruiter",
            "product": "expert product-management recruiter",
            "project_program_mgmt": "expert program-management recruiter",
            "business_ops": "expert business-operations recruiter",
            "consulting": "expert management-consulting recruiter",
            "marketing": "expert marketing recruiter",
            "sales": "expert sales recruiter",
            "hr": "expert HR/talent recruiter",
            # licensed / specialized
            "healthcare": "expert healthcare/clinical recruiter",
            "legal": "expert legal recruiter",
            "design": "expert design recruiter",
            # engineering disciplines
            "mechanical_eng": "expert mechanical-engineering recruiter",
            "civil_eng": "expert civil-engineering recruiter",
            "electrical_eng": "expert electrical-engineering recruiter",
            "construction": "expert construction recruiter",
        }.get(_domain, "expert recruiter")

        # Classify missing keywords so the suggestions model only sees the
        # ones that are safe to recommend the candidate add. Logistics terms
        # (Contract W2, Hybrid) and compliance frameworks (FedRAMP, HIPAA)
        # are excluded from the model-facing list because suggesting their
        # addition either misrepresents the role or encourages fabrication.
        classified = _classify_missing_keywords(det.get("missing_keywords", []))
        safe_missing_skills = classified["skills"]

        facts_block = (
            "=== DETERMINISTIC FACTS (ground truth — reconcile with these) ===\n"
            f"- Hard Skill Match score: {det.get('hard_skill_match', '?')}/100\n"
            f"- Title Alignment score: {det.get('title_alignment', '?')}/100\n"
            f"- Quantified Impact score: {det.get('quantified_impact', '?')}/100\n"
            f"- Section Completeness score: {det.get('section_completeness', '?')}/100\n"
            f"- Recency/Relevance score: {det.get('recency_relevance', '?')}/100\n"
            f"- Readability score: {det.get('readability', '?')}/100\n"
            f"- Format: single-column, standard fonts, no tables/images/columns, "
            "ATS-friendly section headers (Summary, Experience, Skills, "
            "Education, and Projects where applicable), bullets with Unicode standard •\n"
            f"- Missing skills the candidate could plausibly add ({len(safe_missing_skills)}): "
            f"{', '.join(safe_missing_skills[:8]) or 'none'}\n\n"
        )

        suggestion_guardrails = (
            "=== SUGGESTION GUARDRAILS — applies to the 'suggestions' field ===\n"
            "When generating the 'suggestions' list, you MUST NOT recommend any of\n"
            "the following. These are dealbreakers; suggesting them is worse than\n"
            "offering no suggestion at all:\n\n"
            "1. NEVER suggest adding compliance frameworks (FedRAMP, SOC 2, HIPAA,\n"
            "   PCI-DSS, SOX, ISO 27001, NIST 800-53, GDPR, ATO, FISMA, HITRUST)\n"
            "   unless the candidate's existing bullets already demonstrate work in\n"
            "   that domain. If the candidate's experience doesn't show federal,\n"
            "   healthcare, or financial-services work, do not suggest those\n"
            "   framework keywords. The 'missing skills' list above has already\n"
            "   been filtered to exclude these — do not re-introduce them.\n\n"
            "2. NEVER suggest the candidate close a years-of-experience gap by\n"
            "   reframing project work, academic work, or freelance work to look\n"
            "   like full-time employment. If the JD wants 10+ years and the\n"
            "   candidate has 4-5, suggest 'tailor the summary to emphasize depth\n"
            "   of impact and leadership scope' — never suggest inflating tenure\n"
            "   or repackaging projects as professional experience.\n\n"
            "3. NEVER suggest adding JD logistical keywords as resume content.\n"
            "   These include: 'Contract W2', 'Contract', 'Hybrid', 'Remote',\n"
            "   'On-site', 'Full-time', 'Part-time', visa/work-authorization\n"
            "   phrases. These describe the role's logistics, not the candidate's\n"
            "   skills. They have been filtered out of the missing-skills list\n"
            "   above; do not surface them in suggestions.\n\n"
            "4. NEVER suggest adding a job-title noun (e.g., 'Full Stack Developer',\n"
            "   'Software Engineer', 'Senior Engineer') into the skills section.\n"
            "   Titles belong in the experience section; suggesting them as skills\n"
            "   produces keyword-stuffed resumes that recruiters notice.\n\n"
            "5. NEVER suggest fabricating quantitative metrics (percentages, user\n"
            "   counts, revenue figures, team sizes) the candidate cannot defend.\n"
            "   If a bullet lacks impact, suggest 'describe the outcome in concrete\n"
            "   terms' — not 'add a percentage.'\n\n"
            "What you CAN suggest:\n"
            "- Reordering existing bullets to lead with JD-relevant ones\n"
            "- Rewriting weak bullets to surface technical depth that's plausible\n"
            "  given the candidate's stack\n"
            "- Adding real, defensible skills from the missing-skills list above\n"
            "  that the candidate's experience supports (same ecosystem / same\n"
            "  language family / same domain)\n"
            "- Tightening the summary to mention the target role title\n"
            "- Improving headline wording to match the JD's role naming\n\n"
        )

        prompt = (
            f"You are an {_persona} reviewing a tailored resume "
            "against a job description.\n\n"
            + facts_block
            + suggestion_guardrails +
            "Assess:\n"
            "1. experience_relevance (0-100): How closely the candidate's job "
            "titles, industries, and responsibilities match the target role. "
            "Consider career trajectory and transferable skills.\n"
            "2. suggestions: 3-5 specific, actionable improvements.\n"
            "3. strengths: 2-4 strong points of this resume relative to the JD.\n\n"
            "All scores MUST be integers 0-100. All arrays MUST be non-empty.\n\n"
            "Return a JSON object with EXACTLY this structure:\n"
            "{\n"
            '  "experience_relevance": integer,\n'
            '  "suggestions": ["actionable suggestion 1", "suggestion 2", "suggestion 3"],\n'
            '  "strengths": ["strong point 1", "strong point 2"]\n'
            "}\n\n"
            f"=== TAILORED RESUME ===\n{tailored_json}\n\n"
            f"=== JOB DESCRIPTION ANALYSIS ===\n{jd_json}"
        )

        AI_REVIEW_SCHEMA = {
            "experience_relevance": int,
            "suggestions": [str],
            "strengths": [str],
        }

        result = claude_json(
            prompt,
            max_tokens=2000,
            temperature=0.2,
            model=CLAUDE_FLASH,
            schema=AI_REVIEW_SCHEMA,
        )
        result["_available"] = True
        return result

    # ------------------------------------------------------------------
    # Combine
    # ------------------------------------------------------------------

    def _combine(
        self,
        det: dict,
        ai: dict,
        pdf_check: dict | None,
        vendor_compat: dict,
        render_error: str | None,
    ) -> dict:
        """Merge dimensions into the v2 ATS_SCORES_SCHEMA payload.

        Overall = 100% deterministic:
          hard_skill_match 30% + title_alignment 15% + quantified_impact 15%
          + ats_parseability 15% + section_completeness 10%
          + recency_relevance 8% + readability 7%

        Gates: no PDF → overall ≤ 40 (loud render failure); imperfect
        parseability keeps the overall ≤ parseable+5 cap — a PDF that
        doesn't parse can't claim a 90.
        """
        parseability = int(pdf_check["pdf_parseable_score"]) if pdf_check else 0
        hard_unscored = bool((det.get("hard_skill_detail") or {}).get("unscored"))

        if hard_unscored:
            # No JD keywords → hard_skill is not scored. Renormalize the
            # remaining 70% so a missing JD does not invent a 0 or a 100.
            overall = round(
                (
                    det["title_alignment"] * 0.15
                    + det["quantified_impact"] * 0.15
                    + parseability * 0.15
                    + det["section_completeness"] * 0.10
                    + det["recency_relevance"] * 0.08
                    + det["readability"] * 0.07
                )
                / 0.70
            )
        else:
            overall = round(
                det["hard_skill_match"] * 0.30
                + det["title_alignment"] * 0.15
                + det["quantified_impact"] * 0.15
                + parseability * 0.15
                + det["section_completeness"] * 0.10
                + det["recency_relevance"] * 0.08
                + det["readability"] * 0.07
            )
        if pdf_check is None:
            overall = min(overall, 40)
        elif parseability < 100:
            overall = min(overall, parseability + 5)

        # Merge suggestions: render failure first (it invalidates everything
        # else), then deterministic impact suggestions, then AI.
        all_suggestions: List[str] = []
        if pdf_check is None:
            all_suggestions.append(
                "Your resume PDF failed to render or parse — fix this before "
                f"anything else. ({render_error or 'unknown render error'})"
            )
        all_suggestions.extend(det.get("impact_suggestions", []))
        all_suggestions.extend(ai.get("suggestions", []))
        seen: set = set()
        final_suggestions: List[str] = []
        for s in all_suggestions:
            if not isinstance(s, str):
                continue
            s_lower = s.lower()
            if s_lower not in seen:
                seen.add(s_lower)
                final_suggestions.append(s)
            if len(final_suggestions) >= 8:
                break

        combined = {
            "schema_version": 2,
            "overall": max(0, min(100, overall)),
            "hard_skill_match": det["hard_skill_match"],
            "title_alignment": det["title_alignment"],
            "quantified_impact": det["quantified_impact"],
            "ats_parseability": parseability,
            "section_completeness": det["section_completeness"],
            "recency_relevance": det["recency_relevance"],
            "readability": det["readability"],
            "experience_relevance": (
                int(ai["experience_relevance"])
                if isinstance(ai.get("experience_relevance"), (int, float))
                else 0
            ),
            "vendor_compat": vendor_compat,
            "suggestions": final_suggestions or ["No suggestions available"],
            "strengths": ai.get("strengths", ["Resume submitted for review"]),
            "missing_keywords": det["missing_keywords"],
            "found_keywords": det["found_keywords"],
            "render_error": render_error or "",
            # Legacy aliases — keep old consumers reading sensible values.
            "keyword_match": det["hard_skill_match"],
            "keyword_frequency": det["hard_skill_match"],
            "skills_alignment": det["hard_skill_match"],
            "quantifiable_impact": det["quantified_impact"],
            "bullet_quality": det["readability"],
            "format_score": parseability,
        }

        return validate_and_coerce(combined, ATS_SCORES_SCHEMA)
