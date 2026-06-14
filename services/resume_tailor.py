"""
Resume Tailor — transforms a structured resume JSON to align with a job description.

Input:  structured resume JSON (from ResumeParser) + JD analysis JSON
Output: tailored resume JSON conforming to TAILORED_RESUME_SCHEMA

Enhancements over base tailoring:
  - Keyword gap analysis injected into prompt (missing keywords explicitly listed)
  - Bullet structure enforced: Action + Tech + Impact
  - Role alignment: detects title mismatch and instructs summary bridging
  - Weak verb prohibition
  - Required skill frequency rule (each must appear ≥2 times)
  - Post-tailor integrity guard: immutable field enforcement, hallucination detection,
    project generation rules, correction retry loop

Never fabricates experience. Only rewords, reorders, and emphasizes existing content.
"""
import json
import logging
import re
from typing import Any, Dict, List

from schemas.resume_schemas import (
    TAILORED_RESUME_SCHEMA,
    validate_and_coerce,
    extract_job_titles,
    flatten_skills,
    build_resume_text,
)
from services.integrity_guard import IntegrityGuard
from utils.keyword_normalizer import (
    normalize_single,
    get_all_forms,
)

logger = logging.getLogger(__name__)


# Canonical banned-phrase list. Single source of truth for both the tailor
# prompt and IntegrityGuard's post-hoc check. Buzzwords + over-polished
# corporate filler that make the resume read AI-generated.
BANNED_PHRASES: tuple = (
    "Versatile", "Proficient", "Leverages", "Leverage",
    "extensive experience", "proven track record", "results-driven",
    "passionate", "detail-oriented", "highly skilled", "seasoned",
    "cutting-edge", "innovative", "dynamic", "self-motivated",
    "Adept", "dedicated", "committed to excellence",
    "strong foundation", "strong record", "strong background",
    "robust experience", "hands-on expertise",
    "should have", "candidates should", "the candidate", "this candidate",
    "seeking a challenging",
)


class ResumeTailor:
    _guard = IntegrityGuard()

    def tailor(self, structured_resume: Dict[str, Any], jd_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Tailor a structured resume against a JD analysis.

        Takes structured JSON input (not raw text) to eliminate re-parsing
        and reduce hallucination risk. Runs keyword gap analysis first to
        provide the AI with explicit missing keyword context.

        Post-AI integrity pipeline:
          1. validate_and_coerce (schema types)
          2. IntegrityGuard.enforce (immutable fields, hallucination, project rules)
          3. If hallucinated experience detected: one correction retry
        """
        from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_PREVIEW
        from services.keyword_gap_engine import KeywordGapEngine

        jd_json = json.dumps(jd_analysis, indent=2)
        resume_json = json.dumps(structured_resume, indent=2)

        # --- Keyword gap analysis (deterministic, pre-AI) ---
        gap = KeywordGapEngine().analyze(structured_resume, jd_analysis)
        gap_context = self._build_gap_context(gap)

        # --- Role alignment context ---
        role_context = self._build_role_context(structured_resume, jd_analysis)

        # Build a keyword checklist string the AI must embed
        required = jd_analysis.get("required_skills", [])
        keywords = jd_analysis.get("keywords", [])
        keyword_list = ", ".join(dict.fromkeys(required + keywords))  # deduped, ordered

        # Smart truncation preserves all sections
        resume_payload = self._smart_truncate(resume_json)

        # Attach the JD job title onto the final tailored dict so downstream
        # renderers can populate PDF metadata (Subject) and the post-tailor
        # keyword audit can compare against the right role.
        target_role = (jd_analysis.get("job_title") or "").strip()

        def _finalize(t: Dict[str, Any]) -> Dict[str, Any]:
            t = self._restore_contact(t, structured_resume)
            t = self._ensure_jd_skills_coverage(t, structured_resume, jd_analysis)
            t = self._cap_skills_per_category(t, structured_resume, jd_analysis)
            t = self._allocate_bullets(t, jd_analysis)
            if target_role:
                t["target_role"] = target_role
            # Match-gap report: tell the user which JD requirements they
            # genuinely lack, tiered by interview-defensibility. Computed on the
            # cleaned/allocated resume so gaps reflect the honest document.
            # Never raises (degrades to a safe report internally).
            t["match_gaps"] = self._build_match_gaps(t, jd_analysis)
            return t

        prompt = self._build_tailor_prompt(
            keyword_list, gap_context, role_context, resume_payload, jd_json,
        )

        # Intentionally DO NOT pass schema=. Schema-forced tool-use on Claude
        # had two regressions we tracked down empirically:
        #   1. Claude wrapped string fields in JSON envelopes — e.g.
        #      `summary` came out as the literal string `{"summary": "..."}`
        #      because the model treated the schema as "produce JSON-shaped
        #      values" rather than "fill in plain strings".
        #   2. The `projects: [...]` array schema acted as a hint to FILL it
        #      up, so Claude returned 3 invented projects every time. That
        #      broke the downstream pipeline: ContentAugmenter's Phase 1
        #      runs only when `len(projects) < _MAX_PROJECTS=3`, so we
        #      skipped project generation entirely and the user saw whatever
        #      Claude had invented (and IntegrityGuard couldn't strip cleanly
        #      because the model often gave invented projects realistic-
        #      looking names).
        # Plain prompt + multi-strategy JSON parse + validate_and_coerce is
        # how the pre-Claude flow worked; matches that behavior here.
        result = gemini_json(
            prompt=prompt,
            max_tokens=24000,
            temperature=0.4,
            model=GEMINI_PRO,
        )
        # Type coercion remains to fill defaults for entirely missing sections
        validated = validate_and_coerce(result, TAILORED_RESUME_SCHEMA)

        # --- Integrity enforcement ---
        corrected, report = self._guard.enforce(structured_resume, validated, jd_analysis)

        if report.severity == "clean":
            logger.info("Integrity check: clean — no violations detected")
            return _finalize(corrected)

        if report.severity == "auto_fixed":
            logger.warning(
                "Integrity check: auto-fixed — %d immutable overwrites, "
                "%d hallucinated projects removed, %d missing experience re-injected, "
                "%d banned-phrase substitutions, %d unicode-dash substitutions, "
                "%d AI-tell substitutions",
                len(report.immutable_overwrites),
                len(report.hallucinated_projects),
                len(report.missing_experience_reinjected),
                len(report.banned_phrase_substitutions),
                len(report.unicode_dash_substitutions),
                len(report.ai_tell_substitutions),
            )
            return _finalize(corrected)

        # severity == "needs_retry": hallucinated experience detected
        logger.warning(
            "Integrity check: needs retry — %d hallucinated experience entries: %s",
            len(report.hallucinated_experience),
            [e.get("company", "?") for e in report.hallucinated_experience],
        )
        correction_prompt = self._build_correction_prompt(
            structured_resume, jd_analysis, corrected, report
        )
        # Repair path: use GEMINI_PREVIEW for bounded correction only
        retry_result = gemini_json(
            prompt=correction_prompt, 
            max_tokens=24000, 
            temperature=0.3, 
            model=GEMINI_PREVIEW
        )
        retry_validated = validate_and_coerce(retry_result, TAILORED_RESUME_SCHEMA)

        final, retry_report = self._guard.enforce(structured_resume, retry_validated, jd_analysis)

        if retry_report.severity == "needs_retry":
            logger.error(
                "Integrity check: retry still has %d hallucinations — "
                "using auto-corrected version",
                len(retry_report.hallucinated_experience),
            )

        return _finalize(final)

    def regenerate(
        self,
        structured_resume: Dict[str, Any],
        current_tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        user_feedback: str,
    ) -> Dict[str, Any]:
        """Regenerate a tailored resume incorporating user feedback.

        Uses the same integrity pipeline as tailor(), but appends the user's
        specific instructions to the prompt. The base tailoring rules are
        preserved — user feedback is additive guidance, not a replacement.

        Args:
            structured_resume: Original parsed resume (source of truth for
                               immutable fields and hallucination detection).
            current_tailored:  The current tailored version the user wants improved.
            jd_analysis:       Structured JD analysis dict.
            user_feedback:     Free-text instructions from the user describing
                               what to change (e.g., "make summary shorter",
                               "emphasize cloud skills more").

        Returns:
            Regenerated tailored resume dict conforming to TAILORED_RESUME_SCHEMA.
        """
        from services.gemini_client import gemini_json, GEMINI_PRO

        current_json = json.dumps(current_tailored, indent=2)
        original_json = json.dumps(structured_resume, indent=2)
        jd_json = json.dumps(jd_analysis, indent=2)

        # Smart truncation
        current_payload = self._smart_truncate(current_json)
        original_payload = self._smart_truncate(original_json)

        prompt = (
            "You are a professional resume writer. The user has already received a tailored "
            "resume but wants specific changes. Apply their feedback while maintaining all "
            "existing quality and ATS optimization.\n\n"
            "USER'S FEEDBACK (apply these changes):\n"
            f'"{user_feedback}"\n\n'
            "RULES:\n"
            "1. Apply the user's requested changes faithfully.\n"
            "2. Keep all other content that the user did NOT ask to change.\n"
            "3. IMMUTABLE FIELDS — copy these EXACTLY from the ORIGINAL resume:\n"
            "   - contact.name, contact.email, contact.phone, contact.linkedin, contact.github\n"
            "   - Each experience entry's: company, title, location, dates\n"
            "   - Each education entry's: institution, degree, location, dates, gpa\n"
            "4. NEVER fabricate experience, companies, metrics, or skills not in the original.\n"
            "5. certifications: COPY the certifications array EXACTLY from the ORIGINAL resume.\n"
            "   Do NOT add, remove, modify, or reorder any entries. The system enforces this.\n"
            "6. Every field must be a non-null string or array — never null.\n"
            "7. Return the COMPLETE resume JSON — not just the changed parts.\n\n"
            f"=== CURRENT TAILORED RESUME ===\n{current_payload}\n\n"
            f"=== ORIGINAL RESUME (source of truth) ===\n{original_payload}\n\n"
            f"=== JOB DESCRIPTION ANALYSIS ===\n{jd_json}\n\n"
            "Return the full regenerated resume as a JSON object with the same structure."
        )

        # See main tailor call above for the rationale on omitting schema=.
        result = gemini_json(
            prompt=prompt,
            max_tokens=24000,
            temperature=0.4,
            model=GEMINI_PRO,
        )
        validated = validate_and_coerce(result, TAILORED_RESUME_SCHEMA)

        # Integrity enforcement
        corrected, report = self._guard.enforce(structured_resume, validated, jd_analysis)
        if report.severity != "clean":
            logger.warning("Regenerate integrity: %s", report.severity)

        return self._restore_contact(corrected, structured_resume)

    @staticmethod
    def _restore_contact(
        tailored: Dict[str, Any], original: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Forcefully restore original contact fields onto tailored output.

        The LLM sometimes rewrites contact fields despite being told not to.
        This guarantees the original parsed values (especially LinkedIn/GitHub
        from PDF annotations) are always preserved.
        """
        original_contact = original.get("contact", {})
        if original_contact:
            tailored["contact"] = dict(original_contact)  # full copy
            logger.info("Tailor: restored original contact fields")
        return tailored

    # NOTE: a former _strip_fabricated_langs step (which deleted ungrounded
    # concrete languages like Go/Rust/C# from skills) was removed. It contradicted
    # the tailor prompt, which now instructs the model to ADD defensible adjacent
    # languages (e.g. C# for a strong Python dev) — a correct, interview-defensible
    # addition that passes ATS. Language defensibility is a JUDGMENT call and
    # therefore the model's job ("model decides, code guards floors"); the only
    # deterministic skill floors are the binary cert block and the narrow
    # non-skill/soft-trait strip. A truly cross-domain language with no bridge is
    # forbidden by the prompt and surfaced to the user by the match-gap reporter.

    # Maximum items per skills category — keeps the rendered block to ~2
    # lines per category at 10pt with 11mm L/R margins. The LLM sometimes
    # produces categories with 20-25 items which overflows the page; the
    # ATS scorer reads from JSON, so the cap also applies to JSON for
    # consistency between rendered PDF and structured data. Tighter cap
    # applies when there are 2+ projects to make room.
    _MAX_SKILLS_PER_CATEGORY_BASE = 14
    _MAX_SKILLS_PER_CATEGORY_DENSE = 10

    @staticmethod
    def bullets_per_project(project_count: int) -> int:
        """Project bullets scale inversely with project count so total
        project content stays within the one-page A4 budget at 10pt.

        Mapping (verified locally — each row keeps body at 10pt):
            1 project  -> 5 bullets
            2 projects -> 3 bullets
            3 projects -> 3 bullets

        Applies to ANY user / any JD — the tailor prompt, the project
        generator, and the final normalization all use this same rule.
        """
        if project_count <= 1:
            return 5
        return 3

    @classmethod
    def _max_skills_per_category(cls, project_count: int) -> int:
        """Tighter skills cap when there are 2+ projects, to compensate
        for the extra vertical space projects consume."""
        if project_count <= 1:
            return cls._MAX_SKILLS_PER_CATEGORY_BASE
        return cls._MAX_SKILLS_PER_CATEGORY_DENSE

    _MAX_SKILLS_PER_CATEGORY = _MAX_SKILLS_PER_CATEGORY_BASE  # legacy alias

    @staticmethod
    def _allocate_bullets(
        tailored: Dict[str, Any], jd_analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Select the page-fitting subset of model-proposed bullets by relevance.

        Replaces the old fixed per-slot caps (`_cap_project_bullets` +
        `bullets_per_project`). The model now over-generates ranked bullets and
        `bullet_allocator.allocate_bullets` keeps the top-ranked set that fits
        one page at 10pt, favoring experience over projects and keeping a floor
        of 2 bullets per entry. See services/bullet_allocator.py.
        """
        from services.bullet_allocator import allocate_bullets
        from services.resume_renderer import ResumeRenderer

        return allocate_bullets(
            tailored, ResumeRenderer(), jd_analysis=jd_analysis
        )

    @staticmethod
    def _build_match_gaps(
        tailored: Dict[str, Any], jd_analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """User-facing match-gap report (tiered by interview-defensibility).

        Thin wrapper over services.match_gap_reporter.build_match_gaps, which
        runs deterministic gap detection and (only if gaps exist) a small Claude
        call to tier them. Never raises. See services/match_gap_reporter.py.
        """
        from services.match_gap_reporter import build_match_gaps

        return build_match_gaps(tailored, jd_analysis)

    @classmethod
    def _cap_skills_per_category(
        cls,
        tailored: Dict[str, Any],
        original: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Trim each skills category to a max length, preferring items that
        match JD required_skills and keywords (highest ATS value)."""
        skills = tailored.get("skills", {})
        if not isinstance(skills, dict):
            return tailored

        project_count = len(tailored.get("projects") or [])
        cap = cls._max_skills_per_category(project_count)

        jd_terms_lower = set()
        for s in (jd_analysis.get("required_skills") or []):
            jd_terms_lower.add((normalize_single(s) or s).lower())
            for f in get_all_forms(s) or []:
                jd_terms_lower.add(f.lower())
        for s in (jd_analysis.get("keywords") or []):
            jd_terms_lower.add((normalize_single(s) or s).lower())
            for f in get_all_forms(s) or []:
                jd_terms_lower.add(f.lower())

        for cat, items in list(skills.items()):
            if not isinstance(items, list) or not items:
                continue
            # Stable partition: JD-aligned first, then originals. Preserve
            # input order within each group so the highest-relevance items
            # the LLM put first stay first. Applied even when under the cap so
            # short categories still lead with JD-relevant skills (ATS scan).
            jd_aligned = []
            rest = []
            for s in items:
                lower = (normalize_single(s) or s).lower()
                forms = {f.lower() for f in (get_all_forms(s) or [])}
                forms.add(lower)
                if forms & jd_terms_lower:
                    jd_aligned.append(s)
                else:
                    rest.append(s)
            skills[cat] = (jd_aligned + rest)[:cap]
        tailored["skills"] = skills
        return tailored

    @classmethod
    def _ensure_jd_skills_coverage(
        cls,
        tailored: Dict[str, Any],
        original: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Clean the model's skills section — does NOT inject JD phrases.

        HISTORY (why this no longer injects): this method used to paste ALL of
        jd_analysis["required_skills"] (+ top keywords) into the skills section
        to drive the Skills Alignment scorer, which credited a requirement only
        when its literal phrase sat in the skills list. That scorer is now fixed
        (resume_scorer._skills_alignment_score credits a requirement backed
        ANYWHERE in the resume — bullets/summary/skills), so injection no longer
        buys any score. Worse, it ran AFTER IntegrityGuard and re-introduced the
        exact garbage the guard had stripped: JD soft-trait prose ("comfort with
        ambiguity", "collaborative working style"), bare domains ("clinical
        workflows", "EHR integration"), and vague phrases — because production JD
        `required_skills` lists contain all of those. A human recruiter rejects a
        skills section that mirrors the JD verbatim. So injection is DISABLED.

        What this still does (pure cleanup, never additive):
          - Noise-sweep: drop job-title fragments / company name / industry tags
            the MODEL itself copied into a skills category.
          - Soft-trait strip: drop JD culture-fit prose the model pasted in
            (narrow, profession-agnostic; see fabrication_filter).
        Skills CONTENT is the model's job (governed by the prompt); we only
        remove the unambiguous non-skills it occasionally emits.
        """
        skills = tailored.get("skills", {})
        if not isinstance(skills, dict) or not skills:
            return tailored

        # Hard blacklist — production JD analyses pollute keywords with
        # job-title fragments ("Backend Engineer", "Software Engineer II",
        # "Platform Team"), the company name ("Rippling"), and industry
        # tags ("HR Tech"). The model sometimes copies those into skills.
        company_name = (jd_analysis.get("company") or "").strip().lower()
        industry_raw = (jd_analysis.get("industry") or "").strip().lower()
        # Industry often comes as "HR Tech / SaaS" — split on "/" so each
        # half hits the exact-match guard.
        industry_parts = {p.strip() for p in industry_raw.split("/") if p.strip()}
        BLACKLIST_EXACT = {company_name, "saas", "platform team"} | industry_parts
        BLACKLIST_EXACT.discard("")
        BLACKLIST_SUBSTR = (
            "engineer ii",        # title fragment
            "engineer iii",
            "software engineer",
            "backend engineer",
            "frontend engineer",
            "full stack engineer",
            "fullstack engineer",
            "backend services",   # too generic, looks like a label
            "hr tech",
            " team",              # "Platform Team", "Growth Team"
        )

        # Compile word-boundary patterns once so we don't catch "backend
        # engineer" inside "backend engineering" (the discipline is a
        # legitimate skill term; the role title is not).
        noise_patterns = [
            re.compile(r"(?<![a-zA-Z])" + re.escape(s) + r"(?![a-zA-Z])")
            for s in BLACKLIST_SUBSTR
        ]

        def _is_noise(term: str) -> bool:
            t = term.lower().strip()
            if not t or t in BLACKLIST_EXACT:
                return True
            return any(p.search(t) for p in noise_patterns)

        # Sweep: strip noise terms the model put in skills, and prune any
        # category left empty.
        from services.fabrication_filter import is_nonskill_soft_trait

        removed: List[str] = []
        for cat, items in list(skills.items()):
            if not isinstance(items, list):
                continue
            kept = []
            for s in items:
                if isinstance(s, str) and (_is_noise(s) or is_nonskill_soft_trait(s)):
                    removed.append(s)
                else:
                    kept.append(s)
            if kept:
                skills[cat] = kept
            else:
                del skills[cat]

        if removed:
            logger.info(
                "Tailor: stripped %d non-skill terms from skills section: %s",
                len(removed),
                removed,
            )
        tailored["skills"] = skills
        return tailored

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------

    @staticmethod
    def _build_tailor_prompt(
        keyword_list: str,
        gap_context: str,
        role_context: str,
        resume_payload: str,
        jd_json: str,
    ) -> str:
        """Build the main tailoring prompt.

        Structural philosophy:
          - HARD CONSTRAINTS at the top (years honesty, the Defensibility +
            Blendability test that governs all additions, plausibility
            cutoffs, voice). These are the highest-violation rules — they
            sit closest to the goal so the model sees them while planning.
          - IMMUTABLE FIELDS immediately after.
          - CONTENT RULES with bullet counts stated twice (once as a table,
            once inline in the JSON example) so the schema example can't
            silently contradict the rule.
          - BANNED LANGUAGE split into PHRASES vs PATTERNS for salience.
          - FEW-SHOT examples cover the four real failure modes:
            defensible-skill addition, defensible bullet enrichment,
            defensible project enrichment, years-of-experience honesty.
          - JSON SCHEMA last, with bullet array showing the real count.

        The Defensibility + Blendability test replaces the old
        evidence-only rule: the model MAY add skills, sub-tasks, technical
        details, and tools as long as (a) the candidate could defend the
        addition in an interview given their background, and (b) the
        addition blends with existing content (same voice, scope,
        seniority). Cross-ecosystem jumps, fake companies, and seniority
        inflation remain forbidden.
        """
        banned_phrases_str = ", ".join(f"'{p}'" for p in BANNED_PHRASES)

        return (
            "TOP PRIORITY — obey these before everything else (most-violated rules):\n"
            "  1. Output ONE complete, valid JSON object in the exact schema below — never truncate.\n"
            "  2. NEVER fabricate: no certification/license the candidate lacks, no named product they\n"
            "     never used, no JD skill absent from their real resume — even if the JD demands it.\n"
            "  3. Skills = real, correctly-categorized technologies/methods/tools. NEVER list a job\n"
            "     title, a bare domain, a vague catch-all ('AI Coding Tools'), or — worst of all — a\n"
            "     JD soft-trait/working-style phrase ('Ownership and Follow-Through', 'Comfort with\n"
            "     Ambiguity', 'Collaborative Working Style'). Those are not skills; they get the resume\n"
            "     rejected by the recruiter who reads it after the ATS.\n"
            "  4. Every bullet: strong action verb first, dense technical phrasing, a real metric where\n"
            "     the source had one. Preserve every source number.\n"
            "  5. If the candidate is a poor fit for the JD, produce an HONEST resume around their real\n"
            "     strengths — do NOT invent a matching skill set. (Details in the sections below.)\n\n"

            "You are a professional resume writer helping a candidate present their experience clearly.\n"
            "Given the candidate's ACTUAL resume (as structured JSON) and a structured job description analysis,\n"
            "produce a COMPLETE tailored resume as a JSON object.\n\n"

            "YOUR #1 GOAL: Produce a clear, professional, human-sounding resume that naturally aligns with the "
            "job description. The candidate should be able to defend every line in an interview.\n"
            "Naturally incorporate these JD keywords/skills where the candidate has genuinely related "
            f"experience: {keyword_list}\n\n"
            f"{gap_context}"
            f"{role_context}"

            # ----------------------------------------------------------------
            # HARD CONSTRAINTS — the four rules that drive most failures
            # ----------------------------------------------------------------
            "================================================================\n"
            "HARD CONSTRAINTS (violating any of these = the output is rejected)\n"
            "================================================================\n\n"

            "(A) YEARS OF EXPERIENCE — DO NOT INFLATE:\n"
            "  - Compute the candidate's actual years from their experience dates.\n"
            "  - If JD wants N+ years and candidate has N+ → may state specific years ('4+ years').\n"
            "  - If candidate has FEWER → write a GENERIC opener with NO years number at all.\n"
            "    Don't write '3+ years' when JD wants 5+ — that highlights the gap.\n\n"

            "(B) THE DEFENSIBILITY + BLENDABILITY TEST — governs ALL additions:\n"
            "    Applies to skills, sub-tasks inside experience bullets, details in project/\n"
            "    engagement bullets, tools, methods, and any claim — for ANY profession (technical\n"
            "    or non-technical). The test is identical regardless of field.\n"
            "    You MAY add content if BOTH are true:\n"
            "      1. DEFENSIBLE — the candidate could confidently explain it in an interview given\n"
            "         the rest of their background. The addition must fit their actual work, role,\n"
            "         and seniority. A recruiter glancing at it should not be surprised; a hiring\n"
            "         manager asking 'tell me more about that' should get a coherent answer.\n"
            "      2. BLENDABLE — the addition reads as a natural extension of an existing bullet\n"
            "         or skill group: same voice, same scope, same seniority. If you add a JD skill\n"
            "         to the skills section, you should ALSO extend at least one existing bullet to\n"
            "         use it in context — BUT ONLY for skills the candidate actually has. NEVER weave\n"
            "         an ungrounded JD skill (absent from the original resume) into a bullet to\n"
            "         manufacture a match; that is fabrication, not blending.\n"
            "    Examples of GOOD additions, across professions:\n"
            "      - (Engineering) Adding 'AWS Lambda' when the candidate already uses AWS EC2, AND\n"
            "        extending a deployment bullet to mention it for event-driven work.\n"
            "      - (Analytics) Adding 'cohort/retention analysis' to an analyst who already builds\n"
            "        dashboards and writes SQL — a standard extension of that work.\n"
            "      - (Finance) Adding 'variance analysis' to an FP&A analyst who already owns the\n"
            "        monthly forecast — implied by the role.\n"
            "      - (Marketing) Adding 'A/B testing of ad creative' to someone already running\n"
            "        paid campaigns — a natural part of that responsibility.\n"
            "      - (Operations/PM) Adding 'stakeholder reporting cadence' to a PM who already runs\n"
            "        sprints and status updates.\n"
            "    The common thread: each addition is something the candidate plausibly already did\n"
            "    or could speak to — never a different profession's expertise.\n\n"

            "(C) PLAUSIBILITY CUTOFF — these rules apply to EVERY profession (engineering,\n"
            "    business, analytics, healthcare, finance, marketing, operations, design, legal,\n"
            "    etc.), not just technical roles. The principle is the same for all: judge by\n"
            "    whether THIS candidate could defend the addition given their actual background.\n"
            "    Never add:\n"
            "    - A DIFFERENT-ECOSYSTEM / different-domain tool or platform with no analog in the\n"
            "      candidate's real work. NOTE: an ADJACENT skill, tool, language, or method whose\n"
            "      CORE CONCEPTS TRANSFER from what the candidate already does IS DEFENSIBLE and MAY\n"
            "      be presented as a transferable strength — just don't claim deep YEARS in it or\n"
            "      invent a whole project/engagement around it. Examples ACROSS professions:\n"
            "        * Engineer: C# for a strong Python/Java dev; Angular for a React dev.\n"
            "        * Data analyst: Power BI for a strong Tableau user; SQL window functions for\n"
            "          someone already writing complex SQL.\n"
            "        * Finance/accounting: IFRS familiarity for a strong US-GAAP accountant;\n"
            "          a new ERP's reporting module for someone fluent in another ERP's reporting.\n"
            "        * Marketing: paid-social on a new platform for someone running paid-search;\n"
            "          GA4 for a seasoned web-analytics marketer.\n"
            "        * Healthcare/PM/ops: a new EHR or ticketing system for someone who ran an\n"
            "          equivalent one; a new framework (Scrum vs Kanban) for an experienced PM.\n"
            "      The bridge must be REAL (same underlying concepts), not wishful (a different\n"
            "      profession's expertise).\n"
            "    - Domain/compliance claims with no exposure (HIPAA/SOX/PCI/GDPR, clinical\n"
            "      licensure, bar admission, etc. when no matching history exists).\n"
            "    - Seniority signals that contradict reality (leading a team when they were an IC,\n"
            "      owning a budget/P&L when no such context exists).\n"
            "    - New companies, job titles, employment dates, degrees, or certifications. These\n"
            "      are immutable (see next section).\n"
            "    - A SPECIFIC NAMED THIRD-PARTY PRODUCT / VENDOR / SYSTEM the candidate never used —\n"
            "      EVEN IF THE JOB DESCRIPTION LISTS IT. A named commercial product is a verifiable\n"
            "      claim: the candidate either operated it or did not. This holds for ALL fields —\n"
            "      e.g. Qualys, Wiz, ServiceNow, Splunk (tech); Salesforce, HubSpot, Marketo\n"
            "      (sales/marketing); SAP, Oracle Financials, Workday, NetSuite (finance/HR); Epic,\n"
            "      Cerner (healthcare); Tableau, Power BI (analytics). Do NOT inject such a product\n"
            "      into skills or weave it into a bullet unless that exact product already appears\n"
            "      in the ORIGINAL resume. By contrast, GENERIC role-standard skills anyone in the\n"
            "      role can defend (e.g. Git/SQL/REST/Agile for engineers; Excel/forecasting/\n"
            "      stakeholder management for analysts & business roles; charting/segmentation for\n"
            "      marketers) remain fine to add per the Defensibility test.\n"
            "    - CERTIFICATIONS / LICENSES anywhere. Never list a certification or license the\n"
            "      candidate does not hold (e.g. PMP, CPA, AWS Certified, Six Sigma, RN license) —\n"
            "      and never smuggle one into the SKILLS section to dodge this rule (e.g.\n"
            "      'X Implementation Specialist Certification' as a skill). Certifications live ONLY\n"
            "      in the certifications field and are copied verbatim from the original.\n"
            "    - DO NOT copy the JD's required-skill phrases into the SKILLS section unless the\n"
            "      candidate's real history supports them. A skills list that mirrors the JD's\n"
            "      requirements verbatim (e.g. pasting 'CUDA, OpenGL, Medical Device Software\n"
            "      Development' onto a cloud engineer's resume) is keyword-stuffing and is rejected.\n\n"

            "    SORT THE JD'S 'REQUIREMENTS' INTO TWO PILES — this is the key skills decision:\n"
            "    PILE 1 = REAL, NAMED, TEACHABLE SKILLS the JD asks for: a technology, tool,\n"
            "      language, framework, database, or named method/technique (Java, Kafka, GraphQL,\n"
            "      Terraform, pytest; or for non-tech roles: Power BI, variance analysis, GA4, Six\n"
            "      Sigma method, SQL). These are what the ATS actually checks and what a recruiter\n"
            "      expects. You SHOULD ADD these to the skills section when they are DEFENSIBLE for\n"
            "      this candidate — meaning the candidate already has it OR it is genuinely ADJACENT\n"
            "      to something they have (Java for a strong Python/C# dev; GraphQL for someone who\n"
            "      builds REST APIs; Power BI for a strong Tableau user). Adding defensible adjacent\n"
            "      tech is CORRECT, not fabrication — the candidate can speak to it in an interview\n"
            "      because the core concepts transfer. Do NOT add a PILE-1 tech from a DIFFERENT\n"
            "      profession/domain with no bridge (Salesforce for a backend dev; OpenGL for a cloud\n"
            "      engineer) — that has no transferable analog and is not defensible.\n"
            "    PILE 2 = NOT SKILLS — these are WAYS OF WORKING ON skills, value statements, or\n"
            "      role/discipline labels, NOT teachable capabilities:\n"
            "        * Discipline / job-shaped labels: 'Full-Stack Development', 'Software\n"
            "          Engineering', 'Backend Development', 'Data Analysis' (as a bare label).\n"
            "        * Quality / value statements: 'Code Quality', 'Clean and Testable Code', 'Error\n"
            "          Handling and Observability', 'Best Practices', 'Scalability'.\n"
            "        * Soft traits / working style: 'Ownership', 'Comfort with Ambiguity',\n"
            "          'Collaborative Working Style', 'Attention to Detail'.\n"
            "        * Bare domains: 'Clinical Workflows', 'Healthcare', 'FinTech'.\n"
            "        * Vague catch-alls: 'AI Coding Tools', 'Modern Tooling'.\n"
            "      NEVER put PILE 2 in the skills section — a recruiter reads those as keyword soup\n"
            "      and rejects the resume. They belong in the SUMMARY as prose (e.g. a full-stack dev\n"
            "      who owns quality and works well in ambiguity), backed by the PILE-1 skills that\n"
            "      actually deliver them (React + Node + PostgreSQL; pytest + TDD + code review).\n\n"

            "(D) METRICS — PRESERVATION IS MANDATORY, ADDITION IS BOUNDED:\n"
            "    This is the rule the model violates most often. Read it carefully.\n"
            "    1. PRESERVE every number that appears in the source resume. Percentages, latencies,\n"
            "       throughput, counts, ratios, durations, monetary amounts — when you rewrite a\n"
            "       bullet, the NUMBERS from the source MUST survive in the output. If you cannot\n"
            "       fit a number into your rewritten bullet, keep the bullet's original number-\n"
            "       bearing phrasing intact even at the cost of fewer keywords. Losing source\n"
            "       metrics is a failure.\n"
            "       Examples of numbers to preserve: '38%', '120K msgs/sec', '52% MTTD', '2M\n"
            "       prescriptions', '99.9% uptime', '420ms to 95ms', '85% of API surface',\n"
            "       '3 days to 4 hours', etc.\n"
            "    2. You may ADD a metric ONLY when the bullet's nature makes the number inferable\n"
            "       from the source (e.g., 'reduced from 420ms to 95ms' is already there, so adding\n"
            "       the 'p95' qualifier is fine), AND the candidate could defend the number. Do NOT\n"
            "       invent ratios, percentages, or counts that were not in the source.\n"
            "    3. Target: at least 10 bullets in the final resume should contain a numeric metric.\n"
            "       Every number in the source resume must appear somewhere in the output.\n"
            "    4. REALISTIC, HUMAN-WRITTEN NUMERIC FORMATTING — these symbols scream AI-generated\n"
            "       and must not appear in the output:\n"
            "       - `K` and `M` as technical shorthand are FINE when scale warrants ('120K\n"
            "         msgs/sec', '5M users'). What's NOT fine: gluing '+' onto them as hedging.\n"
            "         Write '120K msgs/sec' NOT '120K+ msgs/sec'. Write '2M prescriptions' NOT\n"
            "         '2M+ prescriptions'. Write '85% uptime' NOT '85%+ uptime'.\n"
            "         `+` is reserved for genuine 'or more' semantics already in the source (e.g.,\n"
            "         the source resume says '10+ years' — keep it; don't synthesize new `+`\n"
            "         suffixes).\n"
            "       - NEVER use `~` (tilde) for approximations. Write 'about 5,000' or just '5,000'\n"
            "         — never '~5,000'. Resumes do not hedge.\n"
            "       - NEVER write `sub-Nms` or `sub-N` constructs ('sub-100ms', 'sub-second',\n"
            "         'sub-millisecond'). Write 'under 100ms' or just '100ms'. `sub-` is an AI tic.\n"
            "       - NEVER coin precision-hedging compounds ('near-zero', 'near-perfect',\n"
            "         'near-100%').\n"
            "       - Use commas in large bare numbers when not using K/M: '120,000' not '120000'.\n"
            "       - NEVER use en-dash `–` (U+2013) or em-dash `—` (U+2014) anywhere in\n"
            "         the resume — not in dates, not in bullet text, not in the summary. Use ASCII\n"
            "         hyphen `-` for ranges ('August 2025 - Present') and comma or semicolon\n"
            "         mid-sentence. These Unicode dashes are AI tells; real candidates type ASCII\n"
            "         `-`.\n\n"

            "(E) VOICE — the summary is the CANDIDATE'S self-description on THEIR resume:\n"
            "    - Write in first-person-implied voice (no 'I' pronoun, but describing the\n"
            "      candidate's own work).\n"
            "      Example: 'Backend engineer with hands-on experience building Python microservices on AWS...'\n"
            "    - NEVER write in third-person about 'the candidate' / 'this candidate' / 'candidates should...'\n"
            "    - NEVER open the summary with the JD's job title as the grammatical subject\n"
            "      (e.g. NEVER: 'Software Engineer II Backend candidates should have built...').\n\n"

            # ----------------------------------------------------------------
            # IMMUTABLE FIELDS
            # ----------------------------------------------------------------
            "================================================================\n"
            "IMMUTABLE FIELDS — copy EXACTLY from the original JSON, character for character:\n"
            "================================================================\n"
            "- contact.name, contact.email, contact.phone, contact.linkedin, contact.github, "
            "contact.location, contact.portfolio\n"
            "- Each experience entry's: company, title, location, dates\n"
            "- Each education entry's: institution, degree, location, dates, gpa\n"
            "- certifications: COPY the array EXACTLY from the ORIGINAL resume. Do NOT add, remove, "
            "modify, or reorder entries.\n"
            "Do NOT alter any of these fields, even to fix typos or formatting.\n\n"

            "ALSO IMMUTABLE STRUCTURAL RULES:\n"
            "- The output MUST contain exactly the same number of experience entries as the input.\n"
            "- Projects: include ONLY projects from the original resume in the projects array. If "
            "the original has none, return an empty array []. The backend handles project generation "
            "separately.\n"
            "- Every field in the JSON MUST be a non-null string or array — never null.\n\n"

            # ----------------------------------------------------------------
            # CONTENT RULES
            # ----------------------------------------------------------------
            "================================================================\n"
            "CONTENT RULES (the resume MUST physically fill ONE full A4 page with substantive text)\n"
            "================================================================\n\n"

            "NUMBER RETENTION: see HARD CONSTRAINT (D) — preserve every source number; aim for >=10\n"
            "bullets carrying a metric.\n\n"

            "TERMINAL PUNCTUATION: every bullet AND every sentence in the summary MUST end with a\n"
            "period ('.'). Do NOT end bullets with a semicolon, comma, colon, or no punctuation.\n"
            "Bullet endings are checked deterministically post-generation; missing periods are a\n"
            "visible quality regression.\n\n"

            "BULLET GENERATION — emit RANKED candidates, best-first. A downstream allocator keeps the\n"
            "top-ranked bullets that fit one page and discards the rest, so over-generate and let the\n"
            "STRONGEST bullets lead. Order every bullet list by relevance to THIS job description and\n"
            "by impact (quantified, JD-aligned bullets first).\n"
            "  | Section                | Bullets to emit (ranked, best-first)                |\n"
            "  |------------------------|-----------------------------------------------------|\n"
            "  | Each experience role   | up to 6 bullets, ordered best-first (min 2 real)    |\n"
            "  | Each project           | up to 5 bullets, ordered best-first (min 2 real)    |\n"
            "  | Each bullet length     | ~150-200 chars (2 full lines in rendered PDF)       |\n"
            "  Emit a bullet ONLY if it is a distinct, quantified accomplishment. Do NOT pad to reach a\n"
            "  number and do NOT repeat the same achievement across bullets. Fewer strong bullets beats\n"
            "  more weak ones — the allocator will trim, but it cannot invent quality.\n\n"

            "1. Summary: 3-4 clear, confident sentences. Open by describing what the candidate IS "
            "(e.g. 'Backend engineer with...'). Do NOT start with an adjective or with 'I'. Naturally "
            "weave in the JD-relevant skills the candidate GENUINELY HAS — as many as read naturally "
            "in 3-4 sentences, most important first. Do NOT force a fixed number, do NOT list skills "
            "the candidate lacks (if they lack the JD's core skills, write an honest summary around "
            "their real strengths), and do NOT turn the summary into a keyword list — it must read "
            "like a human wrote it.\n\n"

            "2. Skills: 4-6 categories, most JD-relevant category first, and within each category list the\n"
            "   most JD-relevant skills first. The skills section is read TWICE: by the ATS (which wants\n"
            "   real keyword overlap) and then by a HUMAN recruiter (who rejects keyword soup). Both must\n"
            "   pass. Rules for a HIGH-QUALITY skills section:\n"
            "   - THE LITMUS TEST — before listing any entry, ask: 'Is this a NAMED, teachable\n"
            "     capability that a recruiter would recognize as a hard or methodological skill, and\n"
            "     that I could be tested on?' If not, it does NOT belong in skills. Specifically, NEVER\n"
            "     list any of these (they all read as keyword-stuffing to a recruiter):\n"
            "       * A JOB TITLE — 'C++ Developer', 'Software Engineer', 'Full-Stack Developer',\n"
            "         'Data Analyst'. (A title is not a skill; the underlying tools/methods are.)\n"
            "       * A bare DOMAIN / area phrase — 'Medical Device Software Development', 'Clinical\n"
            "         Workflows', 'Healthcare'. (That's WHERE you worked, not a skill. The skill is the\n"
            "         tech you used there.)\n"
            "       * A VAGUE CATCH-ALL or generic activity — 'AI Coding Tools', 'Software Engineering',\n"
            "         'Code Writing', 'Clean and Testable Code'. (Name the actual thing: 'pytest',\n"
            "         'GitHub Copilot', 'TDD' — not a fuzzy umbrella.)\n"
            "       * A SOFT TRAIT / WORKING-STYLE phrase — 'Ownership and Follow-Through', 'Comfort\n"
            "         with Ambiguity', 'Collaborative Working Style', 'Strong Communication'. These are\n"
            "         JD culture-fit prose. They are NEVER skills. If they matter, they belong in the\n"
            "         SUMMARY as a sentence, never as a skills entry.\n"
            "     If a JD lists such phrases as 'requirements', do NOT mirror them into skills — that is\n"
            "     exactly the keyword-dumping that gets a resume thrown out by a human.\n"
            "   - Each entry that DOES belong is a concrete technology, tool, language, framework, or\n"
            "     named method/technique (e.g. 'PostgreSQL', 'Kubernetes', 'JWT auth', 'A/B testing',\n"
            "     'variance analysis', 'TDD').\n"
            "   - Put every skill in a category it GENUINELY belongs to. Do NOT place a skill in a\n"
            "     mismatched bucket (e.g. OpenGL/CUDA are graphics/GPU, NOT 'Backend & API'; Tableau is\n"
            "     analytics/BI, not 'Languages'; 'Full-Stack Development' is not a 'Backend & API'\n"
            "     entry). If a category label doesn't fit the skills under it, rename the category. Every\n"
            "     category must contain only items that truly match its label.\n"
            "   - Use canonical, recruiter-recognized names ('CI/CD' not 'continuous integration and\n"
            "     continuous delivery'; 'PostgreSQL' not 'postgres db'; 'Kubernetes (EKS)' is fine).\n"
            "   - Include ALL original-resume skills relevant to the JD, experience, or projects — do NOT\n"
            "     drop a real skill just because it isn't in the JD. A skill need NOT appear in a bullet to\n"
            "     be listed, but it MUST be one the candidate genuinely has; do NOT pad with JD wishlist\n"
            "     terms the candidate never used (that is keyword-stuffing and is rejected).\n"
            "   - WHEN THE JD IS LIGHT ON HARD SKILLS (e.g. it asks mostly for traits and ways of\n"
            "     working): do NOT manufacture skills to fill the section. List the candidate's REAL\n"
            "     technical skills, grouped honestly, and let the summary carry the working-style fit.\n"
            "     A shorter, all-real skills section beats a padded one every time.\n\n"

            "3. Education: Institution, degree, dates, location, gpa, coursework — each in its OWN field. "
            "Do NOT concatenate fields (e.g. don't put dates inside the degree string).\n\n"

            "4. Projects: emit up to 5 ranked bullets per project (best-first). The allocator favors "
            "experience over projects when space is tight, so lead each project with its single strongest, "
            "most JD-relevant bullet. Projects in the output array MUST be from the original resume.\n\n"

            "5. Do NOT rely on empty whitespace to fill the page. Generate robust, detailed content for every "
            "bullet and summary sentence.\n\n"

            # ----------------------------------------------------------------
            # BANNED LANGUAGE
            # ----------------------------------------------------------------
            "================================================================\n"
            "BANNED LANGUAGE — do NOT use any of these anywhere in the resume\n"
            "================================================================\n\n"
            f"BANNED PHRASES: {banned_phrases_str}.\n\n"
            "BANNED PATTERNS:\n"
            "  - Third-person about the candidate ('the candidate', 'candidates should have...')\n"
            "  - Opening the summary with the JD's job title as the grammatical subject\n"
            "  - Opening the summary with an adjective ('Versatile...', 'Dynamic...')\n"
            "  - Opening any bullet or summary with 'I'\n"
            "  - AI-tell numeric/dash symbols — see HARD CONSTRAINT (D.4) (no 'K+/M+/%+', '~N',\n"
            "    'sub-N', en/em-dash, or 'near-*' compounds).\n"
            "BAD example: 'Versatile Python FullStack Developer with extensive experience designing scalable systems'\n"
            "GOOD example: 'Full-stack developer with 4+ years building Python backends and React frontends, "
            "focused on API design, cloud deployment, and CI/CD automation.'\n"
            "Write like a real person describing their work — direct, specific, no buzzwords.\n\n"

            # ----------------------------------------------------------------
            # ATS OPTIMIZATION RULES
            # ----------------------------------------------------------------
            "================================================================\n"
            "ATS OPTIMIZATION RULES\n"
            "================================================================\n"
            "1. Mirror the EXACT terminology from the JD (e.g. if JD says 'microservices', use "
            "'microservices' not 'micro-services').\n"
            "2. Put the most JD-relevant skills FIRST in each category.\n"
            "3. Front-load each bullet with a strong action verb that matches the JD's language.\n"
            "4. Weave required keywords naturally into experience bullets and summary — don't just list them.\n"
            "5. The professional summary mentions the target role title and the JD-relevant skills the "
            "candidate genuinely has (see Content Rule 1 — adaptive, grounded, never fabricated).\n"
            "6. Reorder experience bullets so the most JD-relevant achievements appear first.\n"
            "7. For projects, emphasize aspects that directly relate to the JD requirements.\n"
            "8. Each experience bullet starts with an action verb and describes what was done with the "
            "technologies actually used. Include impact or outcome where the original had one.\n"
            "9. Use a diverse mix of action verbs. No single verb may start more than 2 bullets across the "
            "entire resume. Good verbs: Built, Developed, Implemented, Designed, Led, Managed, Created, "
            "Configured, Optimized, Reduced, Migrated, Integrated, Automated, Deployed, Maintained, "
            "Collaborated, Established, Streamlined, Refactored, Monitored. Avoid overusing 'Spearheaded', "
            "'Architected', 'Engineered', or 'Orchestrated' — these sound robotic when used more than once.\n\n"

            # ----------------------------------------------------------------
            # FEW-SHOT EXAMPLES
            # ----------------------------------------------------------------
            "================================================================\n"
            "FEW-SHOT EXAMPLES (Defensibility + Blendability in action)\n"
            "================================================================\n\n"

            "Example A — Defensible skill addition (skill + bullet integration):\n"
            "  JD requires: AWS Lambda, S3, CloudFormation\n"
            "  Candidate's resume: uses AWS EC2, Docker, Python\n"
            "  GOOD: Add 'AWS Lambda, S3' to skills AND extend a deployment bullet →\n"
            "    'Deployed Python services on AWS using EC2 and S3, with Lambda for event-driven processing.'\n"
            "    (Same ecosystem; integrated into a real bullet.)\n"
            "  BAD: Add 'CloudFormation' only to skills section; no bullet mentions it. (Keyword stuffing — not blended.)\n"
            "  BAD: Add 'Azure Functions'. (Cross-ecosystem — not defensible.)\n\n"

            "Example B — Defensible bullet enrichment:\n"
            "  JD requires: API versioning, rate-limiting, authentication\n"
            "  Original bullet: 'Built REST APIs in Python for the customer-facing dashboard.'\n"
            "  GOOD: 'Built REST APIs in Python for the customer-facing dashboard, with versioning, JWT-based\n"
            "    authentication, and rate-limiting for tenant isolation.'\n"
            "    (Defensible — these are standard concerns for any production API; blendable — same voice.)\n"
            "  BAD: 'Built REST APIs in Python, processing 50M requests/day with 99.99% uptime.' (Invented metrics.)\n"
            "  BAD: 'Built REST APIs in Python and led API design across 5 teams.' (Seniority inflation if IC.)\n\n"

            "Example B' — Metric PRESERVATION (see HARD CONSTRAINT D): when rewriting 'reduced response\n"
            "    time from 420ms to 95ms ...', the rewrite MUST keep '420ms' and '95ms'. Never trade a\n"
            "    source number for qualitative wording like 'significantly improved'.\n\n"

            "Example C — Defensible project enrichment:\n"
            "  JD requires: Elasticsearch, search relevance tuning\n"
            "  Original project: 'Built a search feature for an e-commerce site using Elasticsearch.'\n"
            "  GOOD: 'Built a search feature using Elasticsearch with custom analyzers for fuzzy matching and\n"
            "    synonym handling to improve relevance for product queries.'\n"
            "    (Defensible — anyone who built ES search would have done relevance work.)\n\n"

            "Example D — Years-of-experience honesty:\n"
            "  JD wants: 5+ years. Candidate has: ~3 years.\n"
            "  GOOD opener: 'Backend engineer with hands-on experience building Python microservices on AWS...'\n"
            "    (Generic opener, no years cited.)\n"
            "  BAD opener: 'Backend engineer with 5+ years building...' (Inflated.)\n"
            "  BAD opener: 'Backend engineer with 3+ years...' (Honest but highlights the gap — use generic.)\n\n"

            "Example E — NON-TECHNICAL role (the SAME rules apply to every profession):\n"
            "  Role: Financial Analyst. JD requires: variance analysis, Power BI, scenario modeling.\n"
            "  Candidate's resume: owns the monthly forecast in Excel, builds Tableau dashboards.\n"
            "  GOOD: Add 'variance analysis' and 'scenario modeling' to skills AND extend a bullet →\n"
            "    'Owned the monthly revenue forecast in Excel, running variance analysis against actuals and\n"
            "    scenario models to flag budget risk to finance leadership.'\n"
            "    (Defensible — implied by owning the forecast; blendable — same voice. 'Power BI' is\n"
            "    defensible to list as a transferable strength for a strong Tableau user, but do NOT claim\n"
            "    years in it or invent a Power BI project.)\n"
            "  BAD: Add 'CPA' or 'CFA' to skills. (A credential not held — fabrication, regardless of field.)\n"
            "  BAD: Add 'SAP' or 'Oracle Financials' when the candidate never used them. (Named product\n"
            "    they didn't operate — same rule as Qualys/ServiceNow for engineers.)\n\n"

            "Example F — A CLEAN skills section vs. KEYWORD SOUP (this is the #1 skills failure):\n"
            "  JD asks for: backend APIs, ownership and follow-through, comfort with ambiguity,\n"
            "    collaborative working style, experience in clinical/healthcare workflows, AI tooling.\n"
            "  Candidate genuinely has: Python, FastAPI, Kafka, PostgreSQL, Docker, Kubernetes, AWS,\n"
            "    pytest, GitHub Copilot; worked at a health-tech company.\n"
            "  BAD 'Backend & API' category (DO NOT DO THIS):\n"
            "    ['Full-Stack Development', 'Software Engineering', 'AI Coding Tools', 'Clean and\n"
            "     Testable Code', 'Ownership and Follow-Through', 'Comfort with Ambiguity',\n"
            "     'Collaborative Working Style', 'Clinical Workflows']\n"
            "    Why it's rejected: job-title/umbrella phrases ('Full-Stack Development', 'Software\n"
            "    Engineering', 'AI Coding Tools'), JD soft-traits ('Ownership and Follow-Through',\n"
            "    'Comfort with Ambiguity', 'Collaborative Working Style'), and a domain ('Clinical\n"
            "    Workflows') — NONE are backend/API skills. This is keyword soup; a recruiter rejects it.\n"
            "  GOOD — name the REAL skills, bucket them correctly:\n"
            "    'Backend & APIs': ['Python', 'FastAPI', 'REST API design', 'Kafka']\n"
            "    'Data & Infra':   ['PostgreSQL', 'Docker', 'Kubernetes', 'AWS']\n"
            "    'Testing & AI Tooling': ['pytest', 'TDD', 'GitHub Copilot']\n"
            "    The soft-traits ('ownership', 'comfort with ambiguity') and the healthcare domain go\n"
            "    into the SUMMARY as a sentence — never into skills.\n\n"

            # ----------------------------------------------------------------
            # JSON SCHEMA
            # ----------------------------------------------------------------
            "================================================================\n"
            "Return a JSON object with EXACTLY this structure\n"
            "(bullet arrays below show ONE example bullet — the actual counts come from the BULLET-COUNT TABLE above)\n"
            "================================================================\n"
            "{\n"
            '  "contact": {\n'
            '    "name": "Full Name",\n'
            '    "email": "email",\n'
            '    "phone": "phone",\n'
            '    "linkedin": "linkedin url or profile path",\n'
            '    "github": "github url or username",\n'
            '    "location": "City, ST (copy from original)",\n'
            '    "portfolio": "portfolio URL (copy from original)"\n'
            "  },\n"
            '  "summary": "3-4 sentence professional summary, first-person-implied",\n'
            '  "skills": {\n'
            '    "Category Name": ["Skill1", "Skill2", "..."],\n'
            '    "Another Category": ["Skill3", "Skill4", "..."]\n'
            "  },\n"
            '  "experience": [\n'
            "    {\n"
            '      "title": "Job Title",\n'
            '      "company": "Company Name",\n'
            '      "location": "City, State/Country",\n'
            '      "dates": "Start - End",\n'
            '      "type": "Full-time/Internship",\n'
            '      "bullets": [\n'
            '        "Bullet 1 — strongest, most JD-relevant (~150-200 chars)",\n'
            '        "Bullet 2",\n'
            '        "Bullet 3 (up to 6 per role, ranked best-first; min 2 real)",\n'
            '        "... emit only distinct, quantified bullets — no padding ..."\n'
            "      ]\n"
            "    }\n"
            "  ],\n"
            '  "education": [\n'
            "    {\n"
            '      "degree": "Degree name ONLY (e.g. Master of Science, Information Technology) — no dates, institution, or location here",\n'
            '      "institution": "University Name — separate field, not concatenated with degree",\n'
            '      "location": "City, State — separate field",\n'
            '      "dates": "Graduation date or range — separate field",\n'
            '      "gpa": "GPA if available",\n'
            '      "coursework": "Relevant coursework if listed"\n'
            "    }\n"
            "  ],\n"
            '  "projects": [\n'
            "    {\n"
            '      "name": "Project Name",\n'
            '      "dates": "Date Range",\n'
            '      "bullets": [ /* up to 5 ranked bullets, best-first, ~150-200 chars each */ ],\n'
            '      "tech": "Tech1, Tech2, Tech3"\n'
            "    }\n"
            "  ],\n"
            '  "certifications": ["...exact copy from ORIGINAL.certifications, even if empty []..."]\n'
            "}\n\n"
            f"=== ORIGINAL RESUME (STRUCTURED JSON) ===\n{resume_payload}\n\n"
            f"=== JOB DESCRIPTION ANALYSIS ===\n{jd_json}"
        )

    @staticmethod
    def _build_correction_prompt(
        original: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        corrected: Dict[str, Any],
        report,
    ) -> str:
        """Build a targeted correction prompt listing specific violations."""
        violations = []
        for entry in report.hallucinated_experience:
            violations.append(
                f"- You INVENTED experience at '{entry.get('company', '?')}' "
                f"as '{entry.get('title', '?')}' which does NOT exist in the "
                f"original resume. REMOVE IT."
            )

        original_companies = [
            f"  {e.get('company', '?')} — {e.get('title', '?')}"
            for e in original.get("experience", [])
        ]

        corrected_json = json.dumps(corrected, indent=2)[:6000]
        jd_json = json.dumps(jd_analysis, indent=2)[:3000]

        return (
            "Your previous response contained INTEGRITY VIOLATIONS. Fix them.\n\n"
            "VIOLATIONS FOUND:\n" + "\n".join(violations) + "\n\n"
            "The candidate's ACTUAL experience (ONLY these entries are allowed):\n"
            + "\n".join(original_companies) + "\n\n"
            "RULES:\n"
            "- Include ONLY the experience entries listed above. Do NOT invent any others.\n"
            "- Keep all other improvements (summary, skills, bullet rewording).\n"
            "- Return the same JSON structure as before.\n\n"
            f"=== CORRECTED RESUME SO FAR ===\n{corrected_json}\n\n"
            f"=== JOB DESCRIPTION ===\n{jd_json}"
        )

    @staticmethod
    def _smart_truncate(json_str: str, max_chars: int = 8000) -> str:
        """Truncate resume JSON while preserving all top-level sections.

        If the full JSON fits, return it. Otherwise, progressively trim
        bullet counts from experience entries (oldest first) until it fits.
        """
        if len(json_str) <= max_chars:
            return json_str

        data = json.loads(json_str)
        for exp in reversed(data.get("experience", [])):
            bullets = exp.get("bullets", [])
            while len(bullets) > 2 and len(json.dumps(data, indent=2)) > max_chars:
                bullets.pop()
            exp["bullets"] = bullets

        result = json.dumps(data, indent=2)
        return result[:max_chars]  # final safety net

    @staticmethod
    def _build_gap_context(gap: Dict[str, Any]) -> str:
        """Build keyword gap context block for the prompt."""
        coverage = gap.get("coverage_percentage", 0)
        req_missing = gap.get("required_missing", [])
        missing = gap.get("missing_keywords", [])

        if not missing and not req_missing:
            return ""

        parts = [f"KEYWORD GAP ANALYSIS (current resume coverage: {coverage}%):\n"]

        if req_missing:
            parts.append(
                "MISSING REQUIRED SKILLS (incorporate naturally if candidate has related experience): "
                f"{', '.join(req_missing)}\n"
            )

        # General missing (exclude required already listed)
        req_set = set(r.lower() for r in req_missing)
        general_missing = [kw for kw in missing if kw.lower() not in req_set]
        if general_missing:
            parts.append(
                "MISSING GENERAL KEYWORDS (add where naturally relevant): "
                f"{', '.join(general_missing)}\n"
            )

        parts.append("\n")
        return "".join(parts)

    @staticmethod
    def _build_role_context(
        structured_resume: Dict[str, Any], jd_analysis: Dict[str, Any]
    ) -> str:
        """Build role alignment context if there's a title mismatch."""
        candidate_titles = extract_job_titles(structured_resume)
        jd_title = jd_analysis.get("job_title", "")

        if not candidate_titles or not jd_title:
            return ""

        # Skip bridging if titles already match
        candidate_lower = {t.lower() for t in candidate_titles[:3]}
        jd_lower = jd_title.lower()
        if any(jd_lower in t or t in jd_lower for t in candidate_lower):
            return ""

        return (
            "CROSS-ROLE ALIGNMENT (IMPORTANT — the candidate's background differs from the target role):\n"
            f"Candidate's recent titles: {', '.join(candidate_titles[:3])}\n"
            f"Target role: {jd_title}\n\n"
            "Because the candidate is shifting roles, apply ALL of the following strategies:\n"
            "1. SUMMARY: Write a bridge narrative that explicitly connects the candidate's background "
            f"to the {jd_title} role. Highlight how their existing expertise translates — e.g., "
            "'Cloud Engineer with hands-on full-stack development experience' rather than just "
            "'Cloud Engineer'. Reference specific overlapping technologies.\n"
            "2. EXPERIENCE BULLETS: For each role, reframe bullets to emphasize aspects that are "
            "transferable to the target role. Bring forward any bullets involving technologies or "
            "responsibilities that overlap with the JD. For example, if a Cloud Engineer built "
            "internal dashboards or APIs, lead with those bullets and emphasize the development "
            "work, not just the infrastructure aspect.\n"
            "3. SKILL CATEGORIES: Reorder skill categories so the most target-role-relevant category "
            f"appears first. For a {jd_title} role, lead with languages/frameworks, then tools, "
            "then infrastructure. Within each category, put JD-matching skills first.\n"
            "4. TRANSFERABLE FRAMING: Map the candidate's domain expertise to JD requirements — "
            "e.g., 'CI/CD pipeline design' maps to 'DevOps and deployment automation', "
            "'monitoring and observability' maps to 'production reliability', "
            "'API development' maps to 'backend engineering'.\n"
            "5. Do NOT fabricate experience. Only reframe, reorder, and emphasize existing work. "
            "The candidate's actual companies, titles, and dates must remain unchanged.\n\n"
        )
