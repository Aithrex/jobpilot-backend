"""
Resume Scorer — hybrid ATS scoring combining deterministic and AI-based analysis.

Deterministic layer: alias-aware keyword matching, skill overlap, keyword frequency,
                     bullet metrics, section checks, impact analysis.
AI layer: semantic relevance, scanner simulation, suggestions.

Combined output conforms to ATS_SCORES_SCHEMA.
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
    "fedramp", "soc 2", "soc2", "hipaa", "pci-dss", "pci dss", "pci",
    "sox", "iso 27001", "iso27001", "iso", "nist 800-53", "nist", "gdpr",
    "ccpa", "ato", "fisma", "hitrust", "cmmc",
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
    """Hybrid ATS scorer: deterministic ground truth + AI semantic assessment."""

    def score(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        pdf_bytes: bytes | None = None,
    ) -> Dict[str, Any]:
        """Run full hybrid scoring pipeline. Returns validated ATS scores dict.

        If `pdf_bytes` is provided, the rendered PDF is round-tripped through
        pypdf (the same library real ATSes use) and a deterministic
        parseability score + breakdown is folded into the result. This is
        the strongest available signal for "will real ATSes parse this?"
        — no live ATS API exists, so we verify the text layer instead.
        """
        det = self._deterministic_scores(tailored, jd_analysis)
        # Pass the deterministic results into the AI scorer so the scanner
        # scores reconcile with them rather than being independent guesses.
        ai = self._ai_scores(tailored, jd_analysis, det_facts=det)
        combined = self._combine(det, ai)

        # Surface the classified missing keywords so the UI / API consumer
        # can render them with appropriate framing — skills the candidate
        # could add, logistics terms (informational only), and compliance
        # frameworks (requires domain evidence, not a free addition).
        combined["missing_keywords_classified"] = _classify_missing_keywords(
            det.get("missing_keywords", [])
        )

        if pdf_bytes:
            try:
                from services.pdf_ats_check import check_pdf_parseability
                pdf_check = check_pdf_parseability(pdf_bytes, tailored, jd_analysis)
                combined["pdf_parseable_score"] = pdf_check["pdf_parseable_score"]
                combined["pdf_parseable_checks"] = pdf_check["checks"]
                combined["pdf_parseable_warnings"] = pdf_check["warnings"]
                # Fold parseability into overall — heavy weight because a
                # PDF that doesn't parse means EVERY other ATS metric is
                # moot (the parser never gets to keyword matching).
                if pdf_check["pdf_parseable_score"] < 100:
                    # Cap overall at min(overall, parseable+5) so a 60-parse
                    # resume can't claim a 90 overall.
                    overall_cap = pdf_check["pdf_parseable_score"] + 5
                    combined["overall"] = min(combined["overall"], overall_cap)
            except Exception as e:
                logger.warning("PDF parseability check failed: %s", e)

        return combined

    # ------------------------------------------------------------------
    # Deterministic scoring (pure Python, no AI)
    # ------------------------------------------------------------------

    def _deterministic_scores(self, tailored: Dict[str, Any], jd_analysis: Dict[str, Any]) -> dict:
        """Compute deterministic ATS metrics."""
        required = normalize_keywords(jd_analysis.get("required_skills", []))
        keywords = normalize_keywords(jd_analysis.get("keywords", []))
        all_kw = list(dict.fromkeys(required + keywords))

        resume_text = build_resume_text(tailored)

        kw_score, found_kw, missing_kw = self._keyword_match_score(resume_text, all_kw)
        kw_freq_score = self._keyword_frequency_score(resume_text, all_kw)
        skills_score = self._skills_alignment_score(
            tailored.get("skills", {}), required, resume_text
        )
        section_score = self._section_completeness_score(tailored)
        impact_score = self._quantifiable_impact_score(tailored)
        format_score = 95  # We control the format — always ATS-friendly

        # Impact engine analysis (deterministic bullet quality)
        from services.impact_engine import ImpactEngine
        impact_analysis = ImpactEngine().analyze(tailored)

        return {
            "keyword_match": kw_score,
            "keyword_frequency": kw_freq_score,
            "skills_alignment": skills_score,
            "section_completeness": section_score,
            "quantifiable_impact": impact_score,
            "format_score": format_score,
            "missing_keywords": missing_kw,
            "found_keywords": found_kw,
            "impact_suggestions": impact_analysis.get("suggestions", []),
            "impact_score_detail": impact_analysis.get("impact_score", 0),
        }

    def _keyword_match_score(
        self, resume_text: str, keywords: List[str]
    ) -> Tuple[int, List[str], List[str]]:
        """Alias-aware keyword matching with word-boundary awareness.

        Uses get_all_forms() to check if ANY known alias of a keyword
        appears in the resume text, so "Postgres" in JD matches
        "PostgreSQL" in resume.

        Returns (score_0_100, found_keywords, missing_keywords).
        """
        if not keywords:
            return 100, [], []

        text_lower = resume_text.lower()
        found: List[str] = []
        missing: List[str] = []

        for kw in keywords:
            canonical = normalize_single(kw)
            all_forms = get_all_forms(kw)

            kw_found = False
            for form in all_forms:
                if self._text_contains(text_lower, form):
                    kw_found = True
                    break

            if kw_found:
                found.append(canonical)
            else:
                missing.append(canonical)

        total = len(found) + len(missing)
        score = round((len(found) / total) * 100) if total > 0 else 100
        return score, found, missing

    def _keyword_frequency_score(self, resume_text: str, keywords: List[str]) -> int:
        """Score based on keyword frequency — keywords appearing 2+ times score higher.

        Ideal: each keyword appears at least twice (once in skills, once in bullets).
        Cap at 3 per keyword to prevent gaming.
        """
        if not keywords:
            return 100

        text_lower = resume_text.lower()
        total_freq = 0

        for kw in keywords:
            all_forms = get_all_forms(kw)
            kw_count = 0
            for form in all_forms:
                kw_count += self._count_occurrences(text_lower, form)
            total_freq += min(kw_count, 3)  # Cap at 3 per keyword

        max_possible = len(keywords) * 2  # 2 per keyword is ideal
        return min(100, round((total_freq / max_possible) * 100)) if max_possible > 0 else 100

    def _skills_alignment_score(
        self,
        resume_skills: dict,
        required_skills: List[str],
        resume_text: str = "",
    ) -> int:
        """Percentage of required JD skills the resume DEMONSTRATES.

        A required skill counts as aligned if it is backed ANYWHERE in the
        resume — the skills section, an experience/project bullet, or the
        summary — not only when its literal phrase sits in the skills list.

        Why: the old version credited a requirement only when its exact phrase
        appeared in the skills section. That rewarded keyword-dumping: the
        tailoring model learned to paste JD phrases ("ownership and
        follow-through", "comfort with ambiguity", a bare domain) straight into
        skills to score, producing keyword soup a human recruiter rejects.
        Crediting evidence anywhere removes that incentive — a competency
        demonstrated in a bullet or stated in the summary scores the same as a
        skills-list entry, so the model is free to put each item where it
        honestly belongs. Alias/word-boundary aware via get_all_forms, mirroring
        _keyword_match_score.
        """
        if not required_skills:
            return 100

        # Flatten + normalize the structured skills section (kept for the
        # primary, highest-confidence match).
        flat_skills = set()
        if isinstance(resume_skills, dict):
            for skill_list in resume_skills.values():
                if isinstance(skill_list, list):
                    flat_skills.update(normalize_single(s).lower() for s in skill_list)
        elif isinstance(resume_skills, list):
            flat_skills.update(normalize_single(s).lower() for s in resume_skills)

        text_lower = (resume_text or "").lower()

        def _is_backed(skill: str) -> bool:
            # 1) exact normalized presence in the structured skills section
            if normalize_single(skill).lower() in flat_skills:
                return True
            # 2) any alias of the skill present anywhere in the resume text
            #    (bullets / summary / skills), word-boundary aware
            if text_lower:
                for form in get_all_forms(skill):
                    if self._text_contains(text_lower, form):
                        return True
            return False

        found = sum(1 for skill in required_skills if _is_backed(skill))
        return round((found / len(required_skills)) * 100)

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
        """Score quantifiable impact realistically.

        Old version: min(count * 10, 100) — easily gamed by stuffing any
        two-digit number into every bullet, including AI-tell `K+`/`M+`/`~`.

        New version, still pure-Python:
          1. Count bullets with at least one metric. Base = min(count * 8, 80).
             Cap at 80 so raw count alone never tops the score.
          2. Diversity bonus: +5 per distinct metric *type* beyond the first,
             capped at +20. Five distinct types (percent + count + duration +
             range + multiplier) earns the full +20.
          3. AI-tell penalty: -2 per occurrence of `K+`, `M+`, `%+`, `~N`,
             `sub-N`, `near-X`, capped at -10. Resumes that lean on these
             fake-precision symbols lose ground.
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

        # Diversity: each distinct type beyond the first adds 5, capped at 20.
        diversity_bonus = min(max(len(types_seen) - 1, 0) * 5, 20)

        # AI-tell penalty: -2 per occurrence, capped at -10.
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
        """Word-boundary-aware keyword search in lowercased text."""
        if not keyword_lower:
            return False
        if len(keyword_lower) <= 2:
            return keyword_lower in text_lower
        pattern = re.compile(
            r"(?<![a-zA-Z])" + re.escape(keyword_lower) + r"(?![a-zA-Z])"
        )
        return bool(pattern.search(text_lower))

    @staticmethod
    def _count_occurrences(text_lower: str, keyword_lower: str) -> int:
        """Count occurrences of a keyword in text (word-boundary-aware)."""
        if not keyword_lower:
            return 0
        if len(keyword_lower) <= 2:
            return text_lower.count(keyword_lower)
        pattern = re.compile(
            r"(?<![a-zA-Z])" + re.escape(keyword_lower) + r"(?![a-zA-Z])"
        )
        return len(pattern.findall(text_lower))

    # ------------------------------------------------------------------
    # AI scoring (Claude on Bedrock; routed via the gemini_client facade)
    # ------------------------------------------------------------------

    def _ai_scores(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        det_facts: dict | None = None,
    ) -> dict:
        """Call Claude (via the gemini_client facade — name kept for the
        widely-imported `gemini_json` symbol) for semantic assessment that
        can't be computed deterministically. `det_facts` carries the
        deterministic results so the AI scanner scoring is grounded in what
        we already know to be true (section completeness, keyword coverage,
        format).
        """
        import json
        from services.gemini_client import gemini_json, GEMINI_PRO

        tailored_json = json.dumps(tailored, indent=2)[:6000]
        jd_json = json.dumps(jd_analysis, indent=2)[:3000]

        # Build the deterministic-facts block. Scanner scores must be
        # CONSISTENT with these facts — Claude was guessing wildly before,
        # producing Workday=78 / Taleo=65 even though every fact ATSes
        # actually parse for (sections, keywords, format) was at 95-100.
        facts = det_facts or {}

        # Classify missing keywords so the suggestions model only sees the
        # ones that are safe to recommend the candidate add. Logistics terms
        # (Contract W2, Hybrid) and compliance frameworks (FedRAMP, HIPAA)
        # are excluded from the model-facing list because suggesting their
        # addition either misrepresents the role or encourages fabrication.
        classified = _classify_missing_keywords(facts.get("missing_keywords", []))
        safe_missing_skills = classified["skills"]

        facts_block = (
            "=== DETERMINISTIC PARSING FACTS (use these as evidence) ===\n"
            f"- Keyword Match score: {facts.get('keyword_match', '?')}/100\n"
            f"- Keyword Frequency score: {facts.get('keyword_frequency', '?')}/100\n"
            f"- Skills Alignment score: {facts.get('skills_alignment', '?')}/100\n"
            f"- Section Completeness score: {facts.get('section_completeness', '?')}/100\n"
            f"- Quantifiable Impact score: {facts.get('quantifiable_impact', '?')}/100\n"
            f"- Bullet Quality score: {facts.get('bullet_quality', '?')}/100\n"
            f"- Format: single-column, standard fonts, no tables/images/columns, "
            "ATS-friendly section headers (SUMMARY, EXPERIENCE, PROJECTS, "
            "TECHNICAL SKILLS, EDUCATION), bullets with Unicode standard •\n"
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
            "You are an ATS (Applicant Tracking System) and AI recruitment screener expert.\n"
            "Analyze the tailored resume against the job description.\n\n"
            + facts_block
            + suggestion_guardrails +
            "SCORING RUBRIC — scanner scores MUST track the deterministic facts above. "
            "A resume with Format=ATS-friendly, Section Completeness=100, "
            "Keyword Match≥90, Keyword Frequency≥90, Skills Alignment≥85 should score "
            "85-95 across all six scanners. Lower scores only when there are concrete "
            "issues you can point at (e.g. missing keywords, weak experience-role match).\n\n"
            "1. experience_relevance (0-100): How closely do the candidate's job titles, industries, "
            "and responsibilities match the target role. Consider career trajectory and transferable skills.\n\n"
            "2. scanners — Score each per its known parsing model:\n"
            "   - workday (0-100): Exact keyword matches in skills/experience. If "
            "Keyword Match≥90 and Skills Alignment≥85, score 85-92.\n"
            "   - greenhouse (0-100): Weighs Skills section heavily + structured format. "
            "If Skills Alignment≥85 and format is ATS-friendly, score 85-92.\n"
            "   - lever (0-100): Checks career progression + experience depth. Penalize "
            "only if there's a real progression gap.\n"
            "   - icims (0-100): Strict keyword frequency. If Keyword Frequency≥90, score 85-92.\n"
            "   - taleo (0-100): Format-sensitive — penalizes tables, columns, fancy "
            "headers. This resume is single-column with standard headers, so format "
            "is not a penalty here. Score primarily on keyword coverage; 80-90 if "
            "Keyword Match≥90.\n"
            "   - smartrecruiters (0-100): Semantic + keyword. If Keyword Match≥90 "
            "and Skills Alignment≥85, score 85-92.\n\n"
            "3. ai_screener — Simulate HireVue/Pymetrics-style screening:\n"
            "   - overall (0-100): Overall AI screening pass likelihood\n"
            "   - relevance (0-100): How relevant is the candidate's background\n"
            "   - seniority_fit (0-100): Does experience level match the role\n"
            "   - culture_fit (0-100): Communication style and professional tone\n\n"
            "4. suggestions: List 3-5 specific, actionable improvements the candidate could make.\n\n"
            "5. strengths: List 2-4 strong points of this resume relative to the JD.\n\n"
            "Be REALISTIC but not punitive. Scanner scores must reconcile with the "
            "deterministic facts: if every fact says 'this is ATS-friendly', no scanner "
            "should score below 80 without a specific reason you can cite.\n"
            "All scores MUST be integers 0-100. All arrays MUST be non-empty.\n\n"
            "Return a JSON object with EXACTLY this structure:\n"
            "{\n"
            '  "experience_relevance": integer,\n'
            '  "scanners": { "workday": int, "greenhouse": int, "lever": int, '
            '"icims": int, "taleo": int, "smartrecruiters": int },\n'
            '  "ai_screener": { "overall": int, "relevance": int, '
            '"seniority_fit": int, "culture_fit": int },\n'
            '  "suggestions": ["actionable suggestion 1", "suggestion 2", "suggestion 3"],\n'
            '  "strengths": ["strong point 1", "strong point 2"]\n'
            "}\n\n"
            f"=== TAILORED RESUME ===\n{tailored_json}\n\n"
            f"=== JOB DESCRIPTION ANALYSIS ===\n{jd_json}"
        )

        AI_SCORES_SCHEMA = {
            "experience_relevance": int,
            "scanners": {
                "workday": int,
                "greenhouse": int,
                "lever": int,
                "icims": int,
                "taleo": int,
                "smartrecruiters": int,
            },
            "ai_screener": {
                "overall": int,
                "relevance": int,
                "seniority_fit": int,
                "culture_fit": int,
            },
            "suggestions": [str],
            "strengths": [str],
        }

        return gemini_json(prompt, max_tokens=12000, temperature=0.3, model=GEMINI_PRO, schema=AI_SCORES_SCHEMA)

    # ------------------------------------------------------------------
    # Combine deterministic + AI scores
    # ------------------------------------------------------------------

    def _combine(self, det: dict, ai: dict) -> dict:
        """Merge deterministic and AI scores into the full ATS_SCORES_SCHEMA.

        Overall score weights:
          keyword_match (det):       25%
          keyword_frequency (det):    5%
          skills_alignment (det):    20%
          experience_relevance (ai): 15%
          ai_screener.overall (ai):  10%
          quantifiable_impact (det): 10%
          bullet_quality (det):       5%
          format_score (det):         5%
          section_completeness (det): 5%
        """
        # Extract AI values with safe defaults
        ai_screener = ai.get("ai_screener", {})
        scanners = ai.get("scanners", {})
        experience_relevance = ai.get("experience_relevance", 70)
        ai_overall = ai_screener.get("overall", 70)
        bullet_quality = det.get("impact_score_detail", 70)

        # Compute weighted overall
        overall = round(
            det["keyword_match"] * 0.25
            + det.get("keyword_frequency", 70) * 0.05
            + det["skills_alignment"] * 0.20
            + experience_relevance * 0.15
            + ai_overall * 0.10
            + det["quantifiable_impact"] * 0.10
            + bullet_quality * 0.05
            + det["format_score"] * 0.05
            + det["section_completeness"] * 0.05
        )

        # Merge suggestions: deterministic impact suggestions first, then AI
        ai_suggestions = ai.get("suggestions", ["No suggestions available"])
        impact_suggestions = det.get("impact_suggestions", [])
        all_suggestions = impact_suggestions + ai_suggestions
        # Deduplicate and cap at 8
        seen: set = set()
        final_suggestions: List[str] = []
        for s in all_suggestions:
            s_lower = s.lower()
            if s_lower not in seen:
                seen.add(s_lower)
                final_suggestions.append(s)
            if len(final_suggestions) >= 8:
                break

        combined = {
            "overall": max(0, min(100, overall)),
            "keyword_match": det["keyword_match"],
            "keyword_frequency": det.get("keyword_frequency", 0),
            "skills_alignment": det["skills_alignment"],
            "experience_relevance": experience_relevance,
            "quantifiable_impact": det["quantifiable_impact"],
            "bullet_quality": bullet_quality,
            "format_score": det["format_score"],
            "section_completeness": det["section_completeness"],
            "scanners": scanners,
            "ai_screener": ai_screener,
            "suggestions": final_suggestions,
            "missing_keywords": det["missing_keywords"],
            "strengths": ai.get("strengths", ["Resume submitted for review"]),
        }

        return validate_and_coerce(combined, ATS_SCORES_SCHEMA)
