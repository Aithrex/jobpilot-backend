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
        render_error: str | None = None,
        page_outcome: str | None = None,
    ) -> Dict[str, Any]:
        """Run full hybrid scoring pipeline. Returns validated ATS scores dict.

        If `pdf_bytes` is provided, the rendered PDF is round-tripped through
        pypdf (the same library real ATSes use) and a deterministic
        parseability score + breakdown is folded into the result. This is
        the strongest available signal for "will real ATSes parse this?"
        — no live ATS API exists, so we verify the text layer instead.
        `render_error` / `page_outcome` are accepted from the current caller
        and only used when a PDF is present.
        """
        from services import assessment_service as _assess
        if _assess.enabled():
            try:
                meta = {}
                if isinstance(jd_analysis, dict):
                    meta = jd_analysis.get("_assessment_meta") or {}
                return _assess.score_payload_v2(
                    user_email=str(meta.get("user_email") or ""),
                    guest_id=str(meta.get("guest_id") or ""),
                    record_id=str(meta.get("record_id") or ""),
                    version_id=str(meta.get("version_id") or ""),
                    tailored_resume=tailored,
                    jd_analysis={k: v for k, v in (jd_analysis or {}).items()
                                 if k != "_assessment_meta"},
                    fmt=str(meta.get("format") or "pdf"),
                    idempotency_key=str(meta.get("idempotency_key") or ""),
                    artifact_bytes=pdf_bytes,
                    render_error=render_error,
                    source_resume=meta.get("source_resume")
                    if isinstance(meta.get("source_resume"), dict) else None,
                )
            except Exception as e:
                logger.warning("assessment-v2 path failed, returning partial v2: %s", e)
                return _assess.unavailable_v2_result(reason=str(e))
        from services.tailoring_pipeline import is_result
        if is_result(tailored):
            from services.tailoring_assessment import score_payload
            return score_payload(tailored, jd_analysis, pdf_bytes, render_error)
        det = self._deterministic_scores(tailored, jd_analysis)
        # Pass the deterministic results into the AI scorer so the scanner
        # scores reconcile with them rather than being independent guesses.
        try:
            ai = self._ai_scores(tailored, jd_analysis, det_facts=det)
        except Exception as e:
            logger.warning("AI review pass failed; leaving those checks unavailable: %s", e)
            ai = {}
        combined = self._combine(det, ai, pdf_checked=bool(pdf_bytes))
        if render_error:
            combined["render_error"] = render_error
            combined["pdf_parseable_score"] = 0
            combined["format_score"] = 0

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
                pdf_check = check_pdf_parseability(
                    pdf_bytes, tailored, jd_analysis, outcome=page_outcome,
                )
                combined["pdf_parseable_score"] = pdf_check["pdf_parseable_score"]
                combined["pdf_parseable_checks"] = pdf_check["checks"]
                combined["pdf_parseable_warnings"] = pdf_check["warnings"]
                # Fold parseability into overall — heavy weight because a
                # PDF that doesn't parse means EVERY other ATS metric is
                # moot (the parser never gets to keyword matching).
                if pdf_check["pdf_parseable_score"] < 100 and isinstance(combined.get("overall"), (int, float)):
                    overall_cap = pdf_check["pdf_parseable_score"] + 5
                    combined["overall"] = min(combined["overall"], overall_cap)
            except Exception as e:
                logger.warning("PDF parseability check failed: %s", e)

        return combined

    # ------------------------------------------------------------------
    # Deterministic scoring (pure Python, no AI)
    # ------------------------------------------------------------------

    def _deterministic_scores(self, tailored: Dict[str, Any], jd_analysis: Dict[str, Any]) -> dict:
        """Ledger-driven document coverage. One denominator for required groups.

        Keyword/skills bars both reflect the same required-coverage ledger so
        they cannot disagree. Term presence is retained as a separate advisory
        diagnostic and never mixed into requirement satisfaction.
        """
        from services.requirement_assessment import evaluate_requirements
        from services.requirement_matcher import (
            build_requirement_diagnostics,
            coverage_hits,
            coverage_score,
            scored_term_lists,
        )

        resume_text = build_resume_text(tailored)
        ledger_out = evaluate_requirements(
            document_text=resume_text,
            jd=jd_analysis,
            resume=tailored,
            source_resume=None,
        )
        req_cov = ledger_out.get("required_coverage") or {}
        pref_cov = ledger_out.get("preferred_coverage") or {}
        coverage_pct = req_cov.get("percentage")
        numerator = req_cov.get("numerator")
        denominator = req_cov.get("denominator")

        lists = scored_term_lists(jd_analysis)
        all_kw = list(dict.fromkeys(lists["required"] + lists["keywords"]))
        found_kw, missing_kw, _ = coverage_hits(resume_text, all_kw, resume=tailored)
        term_presence = coverage_score(found_kw, missing_kw)

        section_score = self._section_completeness_score(tailored)
        impact_score = self._quantifiable_impact_score(tailored)
        from services.impact_engine import ImpactEngine
        impact_analysis = ImpactEngine().analyze(tailored)

        ledger_rows = (ledger_out.get("ledger") or {}).get("requirements") or []
        if ledger_rows:
            diagnostics = []
            for row in ledger_rows:
                refs = row.get("document_refs") or []
                span = (refs[0] or {}).get("span") if refs else ""
                section = (refs[0] or {}).get("section") if refs else ""
                diagnostics.append({
                    "jd_text": row.get("display_text") or row.get("requirement_id"),
                    "classification": row.get("priority"),
                    "canonical_identity": row.get("requirement_id"),
                    "matched_span": span,
                    "matched_section": section,
                    "source_support": row.get("source_state"),
                    "match_method": row.get("match_method"),
                    "unmet_qualifiers": row.get("unmet_qualifiers") or [],
                    "document_state": row.get("document_state"),
                    "reason": row.get("reason") or "",
                    "in_denominator": row.get("priority") == "required",
                })
        else:
            diagnostics = [
                d.as_dict() for d in build_requirement_diagnostics(
                    jd_analysis=jd_analysis,
                    document_text=resume_text,
                    resume=tailored,
                )
            ]

        not_shown = []
        for row in ledger_rows:
            if row.get("priority") == "required" and row.get("document_state") in {
                "not_demonstrated", "unknown",
            }:
                not_shown.append(row.get("display_text") or row.get("requirement_id"))

        return {
            # Both bars = same ledger required coverage (no competing math).
            "keyword_match": coverage_pct if coverage_pct is not None else 0,
            "keyword_frequency": coverage_pct if coverage_pct is not None else 0,
            "skills_alignment": coverage_pct if coverage_pct is not None else 0,
            "section_completeness": section_score,
            "quantifiable_impact": impact_score,
            "format_score": None,
            "missing_keywords": not_shown or missing_kw,
            "found_keywords": found_kw,
            "coverage_numerator": numerator,
            "coverage_denominator": denominator,
            "coverage_kind": "required_document_coverage",
            "coverage_status": req_cov.get("status"),
            "coverage_provisional": bool(req_cov.get("provisional")),
            "required_coverage": req_cov,
            "preferred_coverage": pref_cov,
            "term_presence": {
                "label": "Term presence",
                "percentage": term_presence,
                "found": found_kw,
                "missing": missing_kw,
                "denominator": len(found_kw) + len(missing_kw),
                "note": "Advisory search-term presence; separate from requirement satisfaction.",
            },
            "ledger": ledger_out.get("ledger"),
            "evaluations": ledger_out.get("evaluations"),
            "canonical_requirements": ledger_out.get("requirements"),
            "counts": ledger_out.get("counts"),
            "requirement_diagnostics": diagnostics,
            "impact_suggestions": impact_analysis.get("suggestions", []),
            "impact_score_detail": impact_analysis.get("impact_score", 0),
        }

    def _keyword_match_score(
        self, resume_text: str, keywords: List[str]
    ) -> Tuple[int | None, List[str], List[str]]:
        """Canonical presence matching. Repetition does not change the score."""
        from services.requirement_matcher import (
            coverage_hits,
            coverage_score,
            dedupe_synonym_terms,
        )
        terms = dedupe_synonym_terms(keywords)
        found, missing, _ = coverage_hits(resume_text, terms)
        return coverage_score(found, missing), found, missing

    def _keyword_frequency_score(self, resume_text: str, keywords: List[str]) -> int | None:
        """Presence-only. Repeating a term does not increase coverage."""
        score, _, _ = self._keyword_match_score(resume_text, keywords)
        return score

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

        from services.requirement_matcher import match_term_in_text

        def _is_backed(skill: str) -> bool:
            if normalize_single(skill).lower() in flat_skills:
                return True
            if text_lower and match_term_in_text(resume_text, skill).found:
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
        """Call the active LLM for semantic assessment that can't be
        computed deterministically. `det_facts` carries the deterministic
        results so the AI scanner scoring is grounded in what we already
        know to be true (section completeness, keyword coverage, format).
        """
        import json
        from services.claude_client import claude_json, CLAUDE_PRO

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

        return claude_json(
            prompt,
            max_tokens=12000,
            temperature=0.3,
            model=CLAUDE_PRO,
            schema=AI_SCORES_SCHEMA,
        )

    # ------------------------------------------------------------------
    # Combine deterministic + AI scores
    # ------------------------------------------------------------------

    def _combine(self, det: dict, ai: dict, *, pdf_checked: bool = False) -> dict:
        """Document-coverage diagnostic. Not an ATS pass probability.

        Overall is required-term coverage when the denominator is known.
        Frequency is not a scoring factor. Unavailable checks stay null —
        they are not defaulted to 70 or format 95. Vendor scanner numbers are
        not treated as measured results.
        """
        coverage = det.get("skills_alignment")
        if coverage is None:
            coverage = det.get("keyword_match")
        keyword_match = det.get("keyword_match")
        skills_alignment = det.get("skills_alignment")

        ai_suggestions = list(ai.get("suggestions") or [])
        impact_suggestions = det.get("impact_suggestions", [])
        all_suggestions = impact_suggestions + ai_suggestions
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

        # When ledger has no assessable required groups, overall stays unavailable
        # (None) rather than inventing 0% or 100%.
        overall = coverage
        if det.get("coverage_status") in {"unavailable", "incomplete_extraction"}:
            overall = None
        elif coverage is None and det.get("coverage_denominator") == 0:
            overall = None

        combined = {
            "overall": overall if overall is not None else 0,
            "overall_unavailable": overall is None,
            "keyword_match": keyword_match if keyword_match is not None else 0,
            "keyword_frequency": keyword_match if keyword_match is not None else 0,
            "skills_alignment": skills_alignment if skills_alignment is not None else 0,
            "experience_relevance": ai.get("experience_relevance"),
            "quantifiable_impact": det["quantifiable_impact"],
            "bullet_quality": det.get("impact_score_detail"),
            "format_score": det.get("format_score") if pdf_checked else None,
            "section_completeness": det["section_completeness"],
            "suggestions": final_suggestions or ["No suggestions available"],
            "missing_keywords": det["missing_keywords"],
            "found_keywords": det.get("found_keywords") or [],
            "strengths": list(ai.get("strengths") or []),
            "coverage_kind": det.get("coverage_kind") or "required_document_coverage",
            "coverage_numerator": det.get("coverage_numerator"),
            "coverage_denominator": det.get("coverage_denominator"),
            "coverage_status": det.get("coverage_status"),
            "coverage_provisional": det.get("coverage_provisional"),
            "required_coverage": det.get("required_coverage"),
            "preferred_coverage": det.get("preferred_coverage"),
            "term_presence": det.get("term_presence"),
            "ledger": det.get("ledger"),
            "requirement_diagnostics": det.get("requirement_diagnostics") or [],
            "vendor_scores_measured": False,
        }
        # Preserve schema ints for stored records, but do not invent vendor scores.
        if isinstance(ai.get("scanners"), dict) and ai["scanners"]:
            combined["scanners_simulated"] = ai["scanners"]
        if isinstance(ai.get("ai_screener"), dict) and ai["ai_screener"]:
            combined["ai_screener_simulated"] = ai["ai_screener"]
        # Coerce only the historical int fields that are present.
        coerced = validate_and_coerce(
            {k: v for k, v in combined.items() if k in ATS_SCORES_SCHEMA and v is not None},
            {k: v for k, v in ATS_SCORES_SCHEMA.items() if k in combined and combined[k] is not None},
        )
        coerced.update({k: v for k, v in combined.items() if k not in coerced})
        return coerced
