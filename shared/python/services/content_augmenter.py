"""
Content Augmenter — fills sparse resumes to ~90% page fill with projects, bullets, and impact.

Pipeline integration: runs AFTER tailor + integrity guard, BEFORE date normalization and complete_job.
Subsumes the old single-project injection logic.

Phases:
  0: Measure fill → early exit if >= 90% (except project generation, which always runs if < 3)
  1: Generate projects (batch: up to 3 total in ONE LLM call)
  2: Expand experience bullets + baked-in impact metrics
  3: Impact injection on existing no-metric bullets
  4: Overflow protection (trim if > 95%)
  5: ATS hardening (keyword density + summary alignment in PARALLEL, then weak verbs)
  6: Final overflow check after hardening
"""
import copy
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Import METRIC_PATTERN from impact_engine for consistent metric detection
from services.impact_engine import METRIC_PATTERN, WEAK_VERB_PHRASES

# Target fill thresholds
_TARGET_FILL = 0.90
_OVERFLOW_SOFT = 0.95
_MAX_PROJECTS = 3
_MAX_TOTAL_EXP_BULLETS = 20

# Per-role bullet caps by recency (index 0 = most recent)
_ROLE_BULLET_CAPS = [8, 6, 4, 4, 3]  # roles beyond index 4 get cap of 3


class ContentAugmenter:
    """Fills sparse resumes to ~80% page fill with projects, expanded bullets, and impact."""

    def __init__(self, renderer, project_generator):
        self.renderer = renderer
        self.project_generator = project_generator

    # ------------------------------------------------------------------
    # Safe-substitution finalizer
    # ------------------------------------------------------------------

    def _finalize_safe_substitutions(self, tailored: Dict[str, Any]) -> Dict[str, Any]:
        """Re-apply IntegrityGuard's safe-substitution passes after augmentation.

        The tailor pipeline runs IntegrityGuard once after the LLM emits the
        initial tailored resume — that strips AI tells, Unicode dashes,
        banned phrases, and enforces terminal periods. But the augmenter's
        Phase 2 (bullet expansion), Phase 3 (impact injection), and Phase 5
        (ATS hardening) all call the LLM again to rewrite or add bullets.
        Anything those phases emit (tildes, en-dashes, missing periods,
        K+/M+/%+ suffixes) would bypass the original guard.

        This method re-runs the deterministic safe-substitution passes —
        not the full guard (which checks structural integrity and could
        spuriously flag legitimate augmenter additions). Just the cosmetic
        cleanup.
        """
        from services.integrity_guard import IntegrityGuard
        g = IntegrityGuard()
        tailored, _ = g.fix_banned_phrases(tailored)
        tailored, _ = g.fix_unicode_dashes(tailored)
        tailored, _ = g.fix_ai_tell_symbols(tailored)
        tailored, _ = g.fix_terminal_periods(tailored)
        return tailored

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def augment(
        self,
        tailored: Dict[str, Any],
        original: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run all augmentation phases on a tailored resume.

        Args:
            tailored: The tailored resume dict (modified in-place).
            original: The original parsed resume (for grounding).
            jd_analysis: JD analysis dict with required_skills, keywords, etc.

        Returns:
            The augmented tailored resume dict.
        """
        pipeline_start = time.time()

        # Diagnostic — populated through the pipeline; consumed by
        # resume_service.py to surface in the job result.
        diag = {
            "stage_reached": "init",
            "budget_shape": None,
            "generator_returned": 0,
            "rejections": [],
            "final_count": 0,
            "defensive_stripped": [],
            "deep_project_skip": False,
        }
        tailored["_projects_diagnostic"] = diag

        # Edge case: empty skills → likely parsing issue, skip augmentation
        skills = tailored.get("skills", {})
        if not skills or (isinstance(skills, dict) and not any(skills.values())):
            logger.warning("ContentAugmenter: empty skills section — skipping augmentation")
            diag["stage_reached"] = "skipped_empty_skills"
            return tailored

        # ── Defensive strip: remove any project in `tailored` whose name doesn't ──
        # match a project in the ORIGINAL parsed resume. This guards against
        # Claude inventing projects that the IntegrityGuard upstream
        # somehow failed to strip (whatever the upstream bug is, we know
        # from Phase 0 diagnostics it's bypassing the guard for this user's
        # flow). After this strip, Phase 1 below sees the correct count and
        # ProjectGenerator can do its job.
        _orig_proj_keys = {
            (p.get("name", "") or "").lower().strip()
            for p in original.get("projects", [])
        }
        _tail_projects = tailored.get("projects", []) or []
        _real_projects = [
            p for p in _tail_projects
            if (p.get("name", "") or "").lower().strip() in _orig_proj_keys
        ]
        _stripped = len(_tail_projects) - len(_real_projects)
        if _stripped > 0:
            _invented = [
                p.get("name", "?") for p in _tail_projects
                if (p.get("name", "") or "").lower().strip() not in _orig_proj_keys
            ]
            logger.warning(
                "ContentAugmenter: defensive-stripped %d invented project(s) %s "
                "(IntegrityGuard upstream did not catch them)",
                _stripped, _invented,
            )
            diag["defensive_stripped"] = _invented
        tailored["projects"] = _real_projects

        # Phase 0: Measure current fill
        fill = self._measure_fill(tailored)
        _proj_names = [p.get("name", "?") for p in tailored.get("projects", [])]
        _orig_proj_names = [p.get("name", "?") for p in original.get("projects", [])]
        logger.warning(
            "ContentAugmenter Phase 0: fill=%.1f%%, projects=%d %s, original_projects=%d %s, experience=%d",
            fill * 100,
            len(_proj_names), _proj_names,
            len(_orig_proj_names), _orig_proj_names,
            len(tailored.get("experience", [])),
        )

        # Phase 1: ALWAYS try project generation if under cap, regardless of fill.
        # Uses batch generation (single LLM call for all needed projects).
        projects = tailored.get("projects", [])
        if len(projects) < _MAX_PROJECTS:
            t0 = time.time()
            tailored = self._augment_projects(tailored, original, jd_analysis)
            fill = self._measure_fill(tailored)
            logger.warning("ContentAugmenter Phase 1 (projects): fill = %.1f%% [%.1fs]",
                           fill * 100, time.time() - t0)
        else:
            logger.warning("ContentAugmenter Phase 1 SKIPPED: tailor returned %d projects (cap %d) — "
                           "no room for generated projects. This is usually a tailor-prompt issue.",
                           len(projects), _MAX_PROJECTS)

        # Early exit for remaining phases (bullet expansion, impact) if fill is good
        if fill >= _TARGET_FILL:
            logger.info("ContentAugmenter: fill >= 90%% after project gen — skipping bullet expansion")
            # Still trim overflow if page is already too full
            if fill > _OVERFLOW_SOFT:
                tailored = self._overflow_trim(tailored, fill)
            t0 = time.time()
            tailored = self._ats_harden(tailored, jd_analysis)
            logger.info("ContentAugmenter Phase 5 (ATS harden): [%.1fs]", time.time() - t0)
            tailored = self._finalize_safe_substitutions(tailored)
            logger.info("ContentAugmenter: total pipeline time = %.1fs", time.time() - pipeline_start)
            return tailored

        # Phase 2: Expand experience bullets (FLASH model for speed)
        experience = tailored.get("experience", [])
        if experience:
            t0 = time.time()
            tailored = self._expand_bullets(tailored, original, jd_analysis, fill)
            fill = self._measure_fill(tailored)
            logger.info("ContentAugmenter Phase 2 (bullets): fill = %.1f%% [%.1fs]",
                        fill * 100, time.time() - t0)

        # Phase 3: Impact injection on existing no-metric bullets
        t0 = time.time()
        tailored = self._inject_impact(tailored, jd_analysis)
        fill = self._measure_fill(tailored)
        logger.info("ContentAugmenter Phase 3 (impact): fill = %.1f%% [%.1fs]",
                    fill * 100, time.time() - t0)

        # Phase 4: Overflow protection
        tailored = self._overflow_trim(tailored, fill)

        # Phase 5: ATS hardening (parallel where possible)
        t0 = time.time()
        tailored = self._ats_harden(tailored, jd_analysis)
        logger.info("ContentAugmenter Phase 5 (ATS harden): [%.1fs]", time.time() - t0)

        # Phase 6: Final overflow check after hardening
        fill = self._measure_fill(tailored)
        if fill > _OVERFLOW_SOFT:
            tailored = self._overflow_trim(tailored, fill)

        # Phase 7: Safe-substitution finalizer — strip any AI tells, Unicode
        # dashes, missing periods that augmenter LLM calls may have emitted.
        tailored = self._finalize_safe_substitutions(tailored)

        logger.info("ContentAugmenter: final fill = %.1f%%, total pipeline time = %.1fs",
                    fill * 100, time.time() - pipeline_start)
        return tailored

    # ------------------------------------------------------------------
    # Phase 0: Measurement
    # ------------------------------------------------------------------

    def _measure_fill(self, tailored: Dict[str, Any]) -> float:
        """Return page fill ratio 0.0-1.0 using the same font/line-height as
        generate_pdf()'s Pass 1 base settings. Must match the renderer
        exactly so the augmenter's fill estimate aligns with the actual PDF.
        Updated to mirror the renderer's tighter base (10pt body, lh=3.4)
        introduced for the reference-matching density."""
        _, content_h = self.renderer._render_pdf(
            tailored,
            section_gap=self.renderer._MIN_SECTION_GAP,
            entry_gap=self.renderer._MIN_ENTRY_GAP,
            post_header=self.renderer._MIN_POST_HEADER,
            header_gap=self.renderer._MIN_HEADER_GAP,
            skill_gap=self.renderer._MIN_SKILL_GAP,
            body_size=10.0,
            lh=3.4,
            lh_s=3.2,
            measure_only=True,
        )
        return content_h / self.renderer._AVAIL_H

    def _measure_fill_at_smallest_font(self, tailored: Dict[str, Any]) -> float:
        """Same as _measure_fill but at generate_pdf's smallest auto-shrink
        size (9pt body, 3.2 lh, 3.0 lh_s). overflow_trim should only fire
        if content STILL doesn't fit even at this smallest size — otherwise
        the renderer's auto-shrink handles density."""
        _, content_h = self.renderer._render_pdf(
            tailored,
            section_gap=self.renderer._MIN_SECTION_GAP,
            entry_gap=self.renderer._MIN_ENTRY_GAP,
            post_header=self.renderer._MIN_POST_HEADER,
            header_gap=self.renderer._MIN_HEADER_GAP,
            skill_gap=self.renderer._MIN_SKILL_GAP,
            body_size=9.0,
            lh=3.2,
            lh_s=3.0,
            measure_only=True,
        )
        return content_h / self.renderer._AVAIL_H

    # ------------------------------------------------------------------
    # Phase 1: Project generation
    # ------------------------------------------------------------------

    # Small breathing buffer below the renderer's available height. The
    # 4-tier font fallback (10pt-tight → 10pt-ultra → 9.5pt → 9pt) already
    # handles real overflow; this just leaves a hair of room so the chosen
    # shape isn't right at the edge.
    _PROJECT_BUDGET_SAFETY_BUFFER_MM = 3.0

    # Placeholder bullet length representative of what the generator will
    # actually emit (~100-150 chars). Used to measure each candidate shape.
    _BUDGET_PLACEHOLDER_BULLET = (
        "Built a production-grade service in Python using FastAPI and "
        "PostgreSQL with proper observability."
    )

    def _project_budget(
        self,
        tailored: Dict[str, Any],
    ) -> "tuple[int, int]":
        """Decide how many projects (and bullets per project) actually fit.

        Uses ACTUAL measurement via the renderer (not estimates): for each
        candidate shape, renders the resume with placeholder projects of
        that shape and checks whether the total fits the page. This avoids
        the previous per-bullet-height estimates which over-projected by
        ~30-40% and unnecessarily skipped feasible 1x5 / 2x3 shapes.

        Returns (project_count, bullets_per_project). (0, 0) means do not
        generate a projects section — better than silently overflowing.
        """

        avail = self.renderer._AVAIL_H - self._PROJECT_BUDGET_SAFETY_BUFFER_MM

        # Measure the baseline (no projects) for logging context only —
        # the per-shape measurements include everything.
        baseline_copy = copy.deepcopy(tailored)
        baseline_copy["projects"] = []
        _, baseline_h = self.renderer._render_pdf(
            baseline_copy,
            section_gap=self.renderer._MIN_SECTION_GAP,
            entry_gap=self.renderer._MIN_ENTRY_GAP,
            post_header=self.renderer._MIN_POST_HEADER,
            header_gap=self.renderer._MIN_HEADER_GAP,
            skill_gap=self.renderer._MIN_SKILL_GAP,
            body_size=10.0, lh=3.4, lh_s=3.2,
            measure_only=True,
        )

        def _measure_with_shape(count: int, bullets: int) -> float:
            if count == 0:
                return baseline_h
            test = copy.deepcopy(tailored)
            test["projects"] = [{
                "name": f"Placeholder Project {i + 1}",
                "dates": "",
                "tech": "Python, FastAPI, Docker",
                "bullets": [self._BUDGET_PLACEHOLDER_BULLET] * bullets,
            } for i in range(count)]
            _, h = self.renderer._render_pdf(
                test,
                section_gap=self.renderer._MIN_SECTION_GAP,
                entry_gap=self.renderer._MIN_ENTRY_GAP,
                post_header=self.renderer._MIN_POST_HEADER,
                header_gap=self.renderer._MIN_HEADER_GAP,
                skill_gap=self.renderer._MIN_SKILL_GAP,
                body_size=10.0, lh=3.4, lh_s=3.2,
                measure_only=True,
            )
            return h

        # Candidates in priority order — pick the LARGEST one that fits.
        # Matches the existing bullets_per_project rule (1->5, 2-3->3) but
        # extends the bottom with smaller shapes and "no projects at all".
        candidates = (
            (3, 3),
            (2, 3),
            (1, 5),
            (1, 3),
            (0, 0),
        )

        for count, bullets in candidates:
            h = _measure_with_shape(count, bullets)
            if h <= avail:
                logger.warning(
                    "ContentAugmenter._project_budget: baseline=%.1fmm, "
                    "avail=%.1fmm -> %d projects x %d bullets "
                    "(measured total %.1fmm)",
                    baseline_h, avail, count, bullets, h,
                )
                return count, bullets

        # Shouldn't reach here — (0, 0) returns baseline_h which should be
        # <= avail by construction — but be defensive.
        logger.warning(
            "ContentAugmenter._project_budget: baseline already exceeds "
            "available (%.1fmm > %.1fmm) — returning (0, 0)",
            baseline_h, avail,
        )
        return 0, 0

    def _augment_projects(
        self,
        tailored: Dict[str, Any],
        original: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate aligned projects based on the vertical budget that's
        actually left on the page after Experience + Skills + Education.

        Honors the existing "deep project" preservation rule: if the
        candidate already has a project with ≥3 bullets, we don't dilute
        it with additional shallow ones.
        """
        projects = tailored.get("projects", [])
        tailored.setdefault("projects", projects)
        diag = tailored.setdefault("_projects_diagnostic", {
            "stage_reached": "init",
            "budget_shape": None,
            "generator_returned": 0,
            "rejections": [],
            "final_count": 0,
            "defensive_stripped": [],
            "deep_project_skip": False,
        })

        # If user already has at least one deep project, don't dilute it.
        if projects and any(len(p.get("bullets", [])) >= 3 for p in projects):
            logger.warning(
                "ContentAugmenter Phase 1 SKIPPED: candidate has %d project(s) with "
                "≥3 bullets — preserving depth without generating shallow extras",
                len(projects),
            )
            diag["stage_reached"] = "deep_skip"
            diag["deep_project_skip"] = True
            diag["final_count"] = len(projects)
            return tailored

        # Compute how many projects (and bullets each) actually fit.
        target_count, target_bullets = self._project_budget(tailored)
        diag["budget_shape"] = [target_count, target_bullets]

        needed = target_count - len(projects)
        if needed <= 0:
            if target_count == 0:
                logger.warning(
                    "ContentAugmenter Phase 1: budget says 0 projects fit "
                    "on this page — skipping project generation. The resume "
                    "will end at Education without a Projects section "
                    "(better than silently overflowing).",
                )
                diag["stage_reached"] = "budget_zero"
            else:
                logger.warning(
                    "ContentAugmenter: %d projects already present "
                    "(budget target %d) — skipping generation",
                    len(projects), target_count,
                )
                diag["stage_reached"] = "already_at_target"
            diag["final_count"] = len(projects)
            return tailored

        logger.warning(
            "ContentAugmenter Phase 1: generating %d aligned projects "
            "(have %d, budget target %d x %d bullets each)",
            needed, len(projects), target_count, target_bullets,
        )

        # Batch generate all needed projects in ONE LLM call.
        # rejections_sink captures per-project rejection reasons for diagnostics.
        rejections_sink: List[Dict] = []
        generated = self.project_generator.generate_batch(
            count=needed,
            original_resume=original,
            jd_analysis=jd_analysis,
            existing_projects=projects,
            bullets_per_project=target_bullets,
            rejections_sink=rejections_sink,
        )
        diag["generator_returned"] = len(generated)
        diag["rejections"] = rejections_sink

        for proj in generated:
            projects.append(proj)
            logger.warning(
                "ContentAugmenter: injected project '%s' (%d total)",
                proj.get("name", "?"), len(projects),
            )

        tailored["projects"] = projects
        diag["final_count"] = len(projects)
        if len(generated) == 0 and needed > 0:
            # Budget said we should generate N, but generator returned 0.
            # Either the LLM call failed or every candidate got rejected.
            diag["stage_reached"] = (
                "generator_failed" if any(r["name"].startswith("<") for r in rejections_sink)
                else "all_rejected"
            )
        else:
            diag["stage_reached"] = "success"
        return tailored

    @staticmethod
    def _is_duplicate_project(new_project: dict, existing: List[dict]) -> bool:
        """Check if a project is a duplicate by name similarity or tech overlap."""
        new_name = new_project.get("name", "").lower().strip()
        new_tech = set(t.strip().lower() for t in new_project.get("tech", "").split(",") if t.strip())

        for existing_proj in existing:
            ex_name = existing_proj.get("name", "").lower().strip()
            # Name containment check
            if new_name in ex_name or ex_name in new_name:
                return True
            # Tech overlap > 70%
            ex_tech = set(t.strip().lower() for t in existing_proj.get("tech", "").split(",") if t.strip())
            if new_tech and ex_tech:
                overlap = len(new_tech & ex_tech) / max(len(new_tech), len(ex_tech))
                if overlap > 0.7:
                    return True
        return False

    # Stopwords used for normalizing bullet text before the near-duplicate
    # similarity check below. We strip these so that two bullets aren't
    # called "near-duplicate" just because they share "the", "and", "of".
    _STOPWORDS = frozenset({
        "a", "an", "and", "the", "of", "to", "for", "in", "on", "at", "by",
        "with", "from", "as", "is", "it", "this", "that", "into", "via",
        "using", "across", "or", "be", "was", "were", "are",
    })

    @classmethod
    def _tokens(cls, text: str) -> list:
        """Lowercase content-word tokens — minimal cleanup for similarity.

        Kept for backward-compat with any caller; the rejection path no
        longer uses this. We only strip stopwords and very short noise.
        """
        import re as _re
        words = _re.findall(r"[A-Za-z0-9]+", text.lower())
        return [w for w in words if w not in cls._STOPWORDS and len(w) > 2]

    @staticmethod
    def _normalize_for_similarity(text: str) -> str:
        """Lowercase + strip non-alphanumeric for near-duplicate comparison."""
        import re as _re
        return _re.sub(r"[^a-z0-9 ]+", " ", text.lower())

    @classmethod
    def _is_literal_copy(cls, project_text: str, exp_bullet: str) -> tuple:
        """True when project_text is a near-verbatim copy of exp_bullet.

        A portfolio project SHOULD reinforce experience — same domain,
        same tech, same problem area is GOOD. Recruiters use this to
        confirm the candidate actually knows the area. What we reject is
        only the lazy copy-paste: a project bullet whose wording is
        ≥80% identical to an experience bullet at the character level.

        Uses difflib's ratio (character-based) — robust to small edits
        like "Built X" → "Developed X", but catches verbatim re-use.

        Returns (is_copy, ratio).
        """
        from difflib import SequenceMatcher
        a = cls._normalize_for_similarity(project_text).strip()
        b = cls._normalize_for_similarity(exp_bullet).strip()
        if not a or not b:
            return False, 0.0
        # Only meaningful for bullets of similar length. If the project
        # bullet is much shorter or much longer it's not a copy.
        if len(a) < 0.5 * len(b) or len(a) > 2.0 * len(b):
            return False, 0.0
        ratio = SequenceMatcher(None, a, b).ratio()
        return ratio >= 0.80, ratio

    @classmethod
    def _overlaps_experience(
        cls,
        new_project: dict,
        experience: List[dict],
    ) -> tuple:
        """Detect projects that LITERALLY COPY an experience bullet.

        A portfolio project reinforcing the candidate's experience —
        same domain, same tech, same problem area — is exactly what we
        want on a resume. It tells the recruiter the candidate knows
        the area well enough to explore it outside work. The only
        thing to reject is a lazy copy-paste of an experience bullet,
        which signals the model gave up and re-used existing text.

        Detection: any project bullet whose normalized text is ≥80%
        character-identical to any experience bullet.

        Returns (overlaps, reason). reason is human-readable when
        overlaps is True; empty otherwise.
        """
        proj_bullets = [str(b) for b in (new_project.get("bullets") or []) if str(b).strip()]
        if not proj_bullets or not experience:
            return False, ""

        for exp in experience:
            exp_bullets = [str(b) for b in (exp.get("bullets") or []) if str(b).strip()]
            if not exp_bullets:
                continue
            for pb in proj_bullets:
                for i, eb in enumerate(exp_bullets):
                    is_copy, ratio = cls._is_literal_copy(pb, eb)
                    if is_copy:
                        company = (exp.get("company") or "?").strip()
                        return True, (
                            f"literal copy of {company} bullet {i + 1} "
                            f"(similarity {ratio:.2f})"
                        )
        return False, ""

    # ------------------------------------------------------------------
    # Phase 2: Bullet expansion
    # ------------------------------------------------------------------

    def _expand_bullets(
        self,
        tailored: Dict[str, Any],
        original: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        current_fill: float,
    ) -> Dict[str, Any]:
        """Expand experience bullets to fill the page."""
        from services.gemini_client import gemini_json, GEMINI_FLASH
        from schemas.resume_schemas import build_resume_text
        import json

        experience = tailored.get("experience", [])
        if not experience:
            return tailored

        # Calculate total current bullets
        total_current = sum(len(exp.get("bullets", [])) for exp in experience)
        if total_current >= _MAX_TOTAL_EXP_BULLETS:
            return tailored

        # Calculate target bullets per role
        expansion_plan = []
        for i, exp in enumerate(experience):
            current = len(exp.get("bullets", []))
            cap = _ROLE_BULLET_CAPS[i] if i < len(_ROLE_BULLET_CAPS) else 3

            # Short tenure check: cap internships/short roles lower
            title_lower = exp.get("title", "").lower()
            if "intern" in title_lower:
                cap = min(cap, 5)

            needed = max(0, cap - current)
            if needed > 0:
                expansion_plan.append({
                    "index": i,
                    "company": exp.get("company", "Unknown"),
                    "title": exp.get("title", ""),
                    "current_bullets": exp.get("bullets", []),
                    "additional_needed": needed,
                })

        if not expansion_plan:
            return tailored

        # Enforce total cap
        total_additional = sum(p["additional_needed"] for p in expansion_plan)
        budget = _MAX_TOTAL_EXP_BULLETS - total_current
        if total_additional > budget:
            # Scale down proportionally
            scale = budget / total_additional
            for p in expansion_plan:
                p["additional_needed"] = max(1, round(p["additional_needed"] * scale))

        # Build the LLM prompt
        original_text = build_resume_text(original)[:2000]
        required_skills = ", ".join(jd_analysis.get("required_skills", [])[:10])
        keywords = ", ".join(jd_analysis.get("keywords", [])[:10])

        entries_desc = ""
        for p in expansion_plan:
            entries_desc += (
                f"\n- {p['company']} / {p['title']}: "
                f"currently has {len(p['current_bullets'])} bullets, "
                f"generate {p['additional_needed']} more"
            )

        prompt = (
            "You are expanding experience bullets for a resume to fill a one-page PDF.\n\n"
            "STRICT RULES:\n"
            "1. Each bullet MUST be 150-220 characters long (2 full lines on the PDF).\n"
            "2. Each bullet starts with a strong action verb: Built, Developed, Implemented, Designed, "
            "Led, Optimized, Automated, Deployed, Reduced, Integrated, Configured, Streamlined, "
            "Migrated, Established, Refactored, Monitored.\n"
            "3. Each bullet follows: Action Verb + Technology/Context + Measurable Outcome.\n"
            "4. Add REALISTIC approximate metrics to every bullet:\n"
            "   - Use approximations: ~15%, 3x, 50+, 1K+ — never precise numbers\n"
            "   - Scale to context: intern/personal project → small numbers (50+ users, ~200ms)\n"
            "     Small company → medium (1K+ requests, ~20% improvement)\n"
            "     Mid/large company → larger (10K+ users, ~30% reduction)\n"
            "   - Prefer relative improvements (%, Nx) over absolute numbers\n"
            "5. Ground every bullet in the candidate's ACTUAL skills and experience context below.\n"
            "6. Do NOT fabricate companies, clients, or production systems.\n"
            "7. Do NOT duplicate content from existing bullets.\n"
            f"8. Naturally incorporate these JD keywords where relevant: {required_skills}\n"
            f"   Additional keywords: {keywords}\n\n"
            f"Generate additional bullets for these entries:{entries_desc}\n\n"
            f"=== CANDIDATE CONTEXT ===\n{original_text}\n\n"
            "Return a JSON object:\n"
            '{"expanded_experience": [\n'
            '  {"company": "Company Name", "additional_bullets": ["bullet1", "bullet2"]}\n'
            "]}\n"
        )

        EXPANSION_SCHEMA = {
            "expanded_experience": [{
                "company": str,
                "additional_bullets": [str],
            }]
        }

        try:
            result = gemini_json(
                prompt, max_tokens=12000, temperature=0.4,
                model=GEMINI_FLASH, schema=EXPANSION_SCHEMA,
            )
        except Exception as e:
            logger.error("ContentAugmenter: bullet expansion LLM call failed: %s", e)
            return tailored

        # Merge expanded bullets back
        expanded = result.get("expanded_experience", [])
        exp_by_company = {e.get("company", "").lower().strip(): e for e in expanded}

        for p in expansion_plan:
            company_key = p["company"].lower().strip()
            match = exp_by_company.get(company_key)
            if not match:
                continue

            new_bullets = match.get("additional_bullets", [])
            # Filter: non-empty, non-duplicate, reasonable length
            existing_lower = {b.lower() for b in experience[p["index"]].get("bullets", [])}
            for bullet in new_bullets[:p["additional_needed"]]:
                bullet = str(bullet).strip()
                if not bullet or bullet.lower() in existing_lower:
                    continue
                if len(bullet) < 50:
                    continue
                experience[p["index"]]["bullets"].append(bullet)
                existing_lower.add(bullet.lower())

                # Check total cap
                total = sum(len(e.get("bullets", [])) for e in experience)
                if total >= _MAX_TOTAL_EXP_BULLETS:
                    break

            total = sum(len(e.get("bullets", [])) for e in experience)
            if total >= _MAX_TOTAL_EXP_BULLETS:
                break

        return tailored

    # ------------------------------------------------------------------
    # Phase 3: Impact injection on existing bullets
    # ------------------------------------------------------------------

    def _inject_impact(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Add realistic quantifiable metrics to bullets that lack them."""
        from services.gemini_client import gemini_json, GEMINI_FLASH

        # Collect bullets without metrics
        candidates = []
        for section_key in ("experience", "projects"):
            for entry_idx, entry in enumerate(tailored.get(section_key, [])):
                for bullet_idx, bullet in enumerate(entry.get("bullets", [])):
                    if not METRIC_PATTERN.search(bullet):
                        candidates.append({
                            "section": section_key,
                            "entry_index": entry_idx,
                            "bullet_index": bullet_idx,
                            "text": bullet,
                            "context": _get_entry_context(entry),
                        })

        if not candidates:
            logger.info("ContentAugmenter Phase 3: all bullets already have metrics")
            return tailored

        if len(candidates) > 15:
            candidates = candidates[:15]  # Limit to avoid huge prompts

        # Build context signals for realistic scaling
        context_signals = self._build_context_signals(tailored)

        bullets_list = "\n".join(
            f"{i+1}. [{c['context']}] {c['text']}"
            for i, c in enumerate(candidates)
        )

        prompt = (
            "You are adding quantifiable impact to resume bullets that currently lack metrics.\n\n"
            "STRICT RULES FOR REALISTIC METRICS:\n"
            "1. Use ONLY approximate, realistic metrics — never precise numbers.\n"
            "   GOOD: ~15%, 3x, 50+, 1K+, ~200ms, ~$10K\n"
            "   BAD: 14.7%, exactly 1,247 users, $47,832\n"
            "2. Scale metrics to the role context:\n"
            f"   Context signals: {context_signals}\n"
            "   - Intern/junior: 50+ users, ~200ms improvement, 3x faster, ~15% reduction\n"
            "   - Mid-level: 1K+ requests/day, ~30% improvement, 5x throughput\n"
            "   - Senior/lead: 10K+ users, ~40% cost reduction, 100K+ records\n"
            "3. Derive metrics from the ACTION described — don't invent unrelated numbers.\n"
            "   'Built REST API' → 'serving 1K+ requests/day' is reasonable\n"
            "   'Optimized queries' → 'reducing latency by ~40%' is reasonable\n"
            "   'Automated testing' → 'covering 200+ test cases' is reasonable\n"
            "4. NEVER add revenue figures or exact dollar amounts.\n"
            "5. Prefer relative improvements (%, Nx, time saved) over absolute numbers.\n"
            "6. Keep the original meaning intact — only ADD a metric clause, don't rewrite.\n"
            "7. Each enhanced bullet must be under 250 characters.\n\n"
            f"Bullets to enhance:\n{bullets_list}\n\n"
            "Return a JSON object:\n"
            '{"enhanced_bullets": [\n'
            '  {"index": 1, "enhanced": "The full enhanced bullet text"}\n'
            "]}\n"
            "Only include bullets you actually changed. Keep the index numbers matching.\n"
        )

        IMPACT_SCHEMA = {
            "enhanced_bullets": [{
                "index": int,
                "enhanced": str,
            }]
        }

        try:
            result = gemini_json(
                prompt, max_tokens=12000, temperature=0.3,
                model=GEMINI_FLASH, schema=IMPACT_SCHEMA,
            )
        except Exception as e:
            logger.error("ContentAugmenter: impact injection LLM call failed: %s", e)
            return tailored

        # Apply enhanced bullets with validation
        enhanced_map = {}
        for item in result.get("enhanced_bullets", []):
            idx = item.get("index", 0) - 1  # Convert 1-indexed to 0-indexed
            if 0 <= idx < len(candidates):
                enhanced_map[idx] = item.get("enhanced", "")

        applied = 0
        for idx, enhanced_text in enhanced_map.items():
            candidate = candidates[idx]
            original_text = candidate["text"]

            # Validation
            if not enhanced_text or len(enhanced_text) > 250:
                continue
            if len(enhanced_text) < len(original_text) * 0.7:
                continue  # Too much content removed
            if not METRIC_PATTERN.search(enhanced_text):
                continue  # Still no metrics after enhancement

            # Apply
            section = tailored[candidate["section"]]
            entry = section[candidate["entry_index"]]
            entry["bullets"][candidate["bullet_index"]] = enhanced_text
            applied += 1

        logger.info("ContentAugmenter Phase 3: enhanced %d/%d bullets with impact", applied, len(candidates))
        return tailored

    @staticmethod
    def _build_context_signals(tailored: Dict[str, Any]) -> str:
        """Extract seniority/scale context for realistic metric generation."""
        signals = []
        for exp in tailored.get("experience", []):
            title = exp.get("title", "").lower()
            if "intern" in title:
                seniority = "intern"
            elif any(w in title for w in ("junior", "jr", "associate", "entry")):
                seniority = "junior"
            elif any(w in title for w in ("senior", "sr", "lead", "principal", "staff")):
                seniority = "senior"
            elif any(w in title for w in ("manager", "director", "vp", "head")):
                seniority = "leadership"
            else:
                seniority = "mid-level"
            signals.append(f"{exp.get('company', '?')}={seniority}")
        return ", ".join(signals) if signals else "mid-level"

    # ------------------------------------------------------------------
    # Phase 4: Overflow protection
    # ------------------------------------------------------------------

    def _overflow_trim(self, tailored: Dict[str, Any], fill: float) -> Dict[str, Any]:
        """Trim content ONLY if it overflows even at the renderer's smallest
        auto-shrink size (9pt). The renderer's generate_pdf() can shrink
        10pt → 9.5pt → 9pt before bailing. If our 10pt-measured fill says
        we're "overflowing" but the renderer would fit fine at 9pt, trimming
        here just destroys content for no reason. Reference resume the user
        wants to match achieves its density via tight font, not via trimming.
        """
        # Cheap early-out: 10pt fill < 95% means definitely no overflow.
        if fill <= _OVERFLOW_SOFT:
            return tailored

        # Expensive but accurate check: would 9pt rendering overflow?
        fill_at_9pt = self._measure_fill_at_smallest_font(tailored)
        if fill_at_9pt <= 1.0:
            logger.warning(
                "ContentAugmenter Phase 4: fill %.1f%% at 10pt would be %.1f%% at 9pt — "
                "renderer auto-shrink will handle, NO trim.",
                fill * 100, fill_at_9pt * 100,
            )
            return tailored

        logger.warning(
            "ContentAugmenter Phase 4: fill %.1f%% at 10pt / %.1f%% at 9pt — trimming",
            fill * 100, fill_at_9pt * 100,
        )

        # Estimate: each bullet ≈ 1.5-2% of page fill
        _EST_BULLET_FILL = 0.018

        # Strategy 1: trim oldest experience bullets to 3 per role
        experience = tailored.get("experience", [])
        estimated_fill = fill
        for exp in reversed(experience):
            bullets = exp.get("bullets", [])
            while len(bullets) > 3 and estimated_fill > _OVERFLOW_SOFT:
                bullets.pop()
                estimated_fill -= _EST_BULLET_FILL

        # Re-measure
        fill = self._measure_fill(tailored)

        # Strategy 2: trim project bullets (3 → 2)
        if fill > _OVERFLOW_SOFT:
            for proj in tailored.get("projects", []):
                bullets = proj.get("bullets", [])
                while len(bullets) > 2 and estimated_fill > _OVERFLOW_SOFT:
                    bullets.pop()
                    estimated_fill -= _EST_BULLET_FILL
            fill = self._measure_fill(tailored)

        # Strategy 3: trim ONLY the oldest experience role down to 2
        # bullets if still overflowing. Don't strip recent/prior roles
        # below 3 — that's where ATS and recruiters spend the most
        # attention, and an earlier version of this code dropped EVERY
        # role to 2 bullets which produced a sparse, undersized resume.
        if fill > _OVERFLOW_SOFT and len(experience) > 0:
            oldest = experience[-1]
            bullets = oldest.get("bullets", [])
            while len(bullets) > 2 and self._measure_fill(tailored) > _OVERFLOW_SOFT:
                bullets.pop()
                logger.warning(
                    "ContentAugmenter: trimmed oldest-role bullet on %s (now %d)",
                    oldest.get("company", "?"), len(bullets),
                )
            fill = self._measure_fill(tailored)

        # Strategy 4: last resort — remove projects if STILL overflowing.
        # Keeps at least 1 project.
        projects = tailored.get("projects", [])
        if fill > _OVERFLOW_SOFT and len(projects) > 1:
            while len(projects) > 1 and self._measure_fill(tailored) > _OVERFLOW_SOFT:
                removed = projects.pop()
                logger.warning(
                    "ContentAugmenter: removed project '%s' (last-resort trim)",
                    removed.get("name", "?"),
                )

        return tailored

    # ------------------------------------------------------------------
    # Phase 5: ATS hardening
    # ------------------------------------------------------------------

    def _ats_harden(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Ensure the resume passes AI ATS screeners.

        Parallelizes keyword density and summary alignment (they touch
        different resume sections), then runs weak verbs sequentially
        (since it modifies bullets that keyword density may have changed).
        """
        # Step 1: Run keyword density + summary alignment in PARALLEL.
        # These are independent: keyword density modifies bullets,
        # summary alignment modifies the summary field.
        #
        # Timeout handling: a `with ThreadPoolExecutor(...)` block waits on
        # __exit__ for ALL submitted threads, even ones we abandoned via
        # `future.result(timeout=...)`. If keyword density's LLM call
        # hangs (e.g. 503 retries), the per-future timeout fires at 30s but
        # then the context manager blocks for another ~30s waiting for the
        # runaway thread — net ~60s of wall time per timeout for what was
        # already abandoned.
        #
        # Fix: explicit executor lifecycle + shutdown(wait=False) in finally
        # so we return as soon as our useful work is done. Orphan threads
        # finish on their own without blocking this request. Per-future
        # timeouts are unchanged from the original (30s each) — accuracy is
        # preserved: any LLM response that would have completed inside
        # 30s before is still captured now.
        keyword_result = tailored
        summary_result = tailored

        tailored_for_keywords = copy.deepcopy(tailored)
        tailored_for_summary = copy.deepcopy(tailored)

        executor = ThreadPoolExecutor(max_workers=2)
        had_timeout = False
        try:
            future_kw = executor.submit(
                self._enforce_keyword_density, tailored_for_keywords, jd_analysis
            )
            future_summary = executor.submit(
                self._verify_summary_alignment, tailored_for_summary, jd_analysis
            )

            try:
                keyword_result = future_kw.result(timeout=30)
            except FuturesTimeoutError:
                had_timeout = True
                logger.warning(
                    "ContentAugmenter ATS: keyword density timed out (>30s) — abandoning thread"
                )
                keyword_result = tailored
            except Exception as e:
                logger.error("ContentAugmenter ATS: keyword density failed: %s", e)
                keyword_result = tailored

            try:
                summary_result = future_summary.result(timeout=30)
            except FuturesTimeoutError:
                had_timeout = True
                logger.warning(
                    "ContentAugmenter ATS: summary alignment timed out (>30s) — abandoning thread"
                )
                summary_result = tailored
            except Exception as e:
                logger.error("ContentAugmenter ATS: summary alignment failed: %s", e)
                summary_result = tailored
        finally:
            # Don't wait for orphan threads. They finish on their own; the
            # Lambda either reuses the warm container with the thread idle,
            # or the container is recycled. Either way, this request returns
            # immediately instead of holding the user.
            executor.shutdown(wait=False)
            if had_timeout:
                logger.info(
                    "ContentAugmenter ATS: returning early; orphan thread will complete in background"
                )

        # Merge: take experience/projects from keyword result, summary from summary result
        tailored["experience"] = keyword_result.get("experience", tailored.get("experience", []))
        tailored["summary"] = summary_result.get("summary", tailored.get("summary", ""))

        # Step 2: Run weak verbs sequentially (depends on keyword density output)
        tailored = self._fix_weak_verbs(tailored)

        # Step 3: Chronological order (pure Python, instant)
        tailored = self._enforce_chronological_order(tailored)
        return tailored

    def _enforce_keyword_density(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Ensure each required keyword appears >= 2 times in the resume."""
        from services.gemini_client import gemini_json, GEMINI_FLASH
        from schemas.resume_schemas import build_resume_text
        from utils.keyword_normalizer import get_all_forms

        required = jd_analysis.get("required_skills", [])
        if not required:
            return tailored

        resume_text = build_resume_text(tailored).lower()

        under_represented = []
        for skill in required:
            all_forms = get_all_forms(skill)
            total_count = 0
            for form in all_forms:
                if len(form) <= 2:
                    total_count += resume_text.count(form)
                else:
                    pattern = re.compile(r"(?<![a-zA-Z])" + re.escape(form) + r"(?![a-zA-Z])")
                    total_count += len(pattern.findall(resume_text))
            if total_count < 2:
                under_represented.append(skill)

        if not under_represented:
            logger.info("ContentAugmenter ATS: all keywords at sufficient density")
            return tailored

        logger.info(
            "ContentAugmenter ATS: %d under-represented keywords: %s",
            len(under_represented), under_represented[:10],
        )

        # Find the most relevant bullets to weave keywords into
        experience = tailored.get("experience", [])
        bullet_pool = []
        for i, exp in enumerate(experience):
            for j, bullet in enumerate(exp.get("bullets", [])):
                bullet_pool.append({
                    "exp_idx": i,
                    "bullet_idx": j,
                    "text": bullet,
                    "company": exp.get("company", ""),
                })

        if not bullet_pool:
            return tailored

        # Select up to 5 bullets to potentially modify (fewer = smaller output = less truncation risk)
        sample = bullet_pool[:5]
        bullets_text = "\n".join(f"{i+1}. {b['text']}" for i, b in enumerate(sample))

        prompt = (
            "You are integrating missing keywords into existing resume bullets.\n\n"
            "RULES:\n"
            "1. Naturally weave each keyword into a relevant bullet — do NOT just append it.\n"
            "2. Preserve the original meaning and structure of each bullet.\n"
            "3. Keep each bullet under 250 characters.\n"
            "4. Only modify bullets where the keyword fits naturally.\n"
            "5. Each keyword should be added to at least one bullet.\n\n"
            f"Keywords to integrate: {', '.join(under_represented[:10])}\n\n"
            f"Current bullets:\n{bullets_text}\n\n"
            "Return a JSON object:\n"
            '{"modified_bullets": [\n'
            '  {"index": 1, "text": "The modified bullet with keyword integrated"}\n'
            "]}\n"
            "Only include bullets you actually changed.\n"
        )

        KEYWORD_SCHEMA = {"modified_bullets": [{"index": int, "text": str}]}

        try:
            result = gemini_json(
                prompt, max_tokens=24000, temperature=0.3,
                model=GEMINI_FLASH, schema=KEYWORD_SCHEMA,
            )
        except Exception as e:
            logger.error("ContentAugmenter ATS keyword density: LLM call failed: %s", e)
            return tailored

        for item in result.get("modified_bullets", []):
            idx = item.get("index", 0) - 1
            new_text = item.get("text", "")
            if 0 <= idx < len(sample) and new_text and len(new_text) <= 250:
                entry = sample[idx]
                experience[entry["exp_idx"]]["bullets"][entry["bullet_idx"]] = new_text

        return tailored

    def _fix_weak_verbs(self, tailored: Dict[str, Any]) -> Dict[str, Any]:
        """Replace weak opening verbs with strong action verbs."""
        from services.gemini_client import gemini_json, GEMINI_FLASH

        weak_bullets = []
        for section_key in ("experience", "projects"):
            for entry_idx, entry in enumerate(tailored.get(section_key, [])):
                for bullet_idx, bullet in enumerate(entry.get("bullets", [])):
                    bullet_lower = re.sub(r"^[-\u2022*]\s*", "", bullet.lower().strip())
                    for phrase in WEAK_VERB_PHRASES:
                        if bullet_lower.startswith(phrase):
                            weak_bullets.append({
                                "section": section_key,
                                "entry_index": entry_idx,
                                "bullet_index": bullet_idx,
                                "text": bullet,
                                "weak_phrase": phrase,
                            })
                            break

        if not weak_bullets:
            logger.info("ContentAugmenter ATS: no weak verbs found")
            return tailored

        logger.info("ContentAugmenter ATS: %d bullets with weak verbs", len(weak_bullets))

        bullets_text = "\n".join(
            f"{i+1}. {b['text']}" for i, b in enumerate(weak_bullets[:10])
        )

        prompt = (
            "You are replacing weak opening verbs in resume bullets with strong action verbs.\n\n"
            "RULES:\n"
            "1. ONLY change the opening verb/phrase — keep the rest of the bullet identical.\n"
            "2. Use strong verbs: Built, Developed, Implemented, Designed, Led, Optimized, "
            "Automated, Deployed, Reduced, Integrated, Configured, Streamlined, Migrated, "
            "Established, Refactored, Monitored, Engineered, Architected, Spearheaded.\n"
            "3. Choose the verb that best matches the action described.\n"
            "4. Do NOT change the content or meaning of the bullet.\n\n"
            f"Bullets to fix:\n{bullets_text}\n\n"
            "Return a JSON object:\n"
            '{"fixed_bullets": [\n'
            '  {"index": 1, "text": "The bullet with strong opening verb"}\n'
            "]}\n"
        )

        VERB_SCHEMA = {"fixed_bullets": [{"index": int, "text": str}]}

        try:
            result = gemini_json(
                prompt, max_tokens=12000, temperature=0.2,
                model=GEMINI_FLASH, schema=VERB_SCHEMA,
            )
        except Exception as e:
            logger.error("ContentAugmenter ATS weak verbs: LLM call failed: %s", e)
            return tailored

        for item in result.get("fixed_bullets", []):
            idx = item.get("index", 0) - 1
            new_text = item.get("text", "")
            if 0 <= idx < len(weak_bullets) and new_text:
                wb = weak_bullets[idx]
                tailored[wb["section"]][wb["entry_index"]]["bullets"][wb["bullet_index"]] = new_text

        return tailored

    def _verify_summary_alignment(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Ensure summary mentions job title and >= 3 required skills."""
        from services.gemini_client import gemini_json, GEMINI_FLASH

        summary = tailored.get("summary", "")
        if not summary:
            return tailored

        job_title = jd_analysis.get("job_title", "").lower()
        required = [s.lower() for s in jd_analysis.get("required_skills", [])[:5]]

        summary_lower = summary.lower()
        has_title = job_title in summary_lower if job_title else True
        skills_in_summary = sum(1 for s in required if s in summary_lower)

        if has_title and skills_in_summary >= 3:
            logger.info("ContentAugmenter ATS: summary alignment OK")
            return tailored

        logger.info(
            "ContentAugmenter ATS: summary needs alignment (title=%s, skills=%d/3)",
            has_title, skills_in_summary,
        )

        # Get candidate's actual background for grounding
        experience = tailored.get("experience", [])
        exp_context = ", ".join(
            f"{e.get('title', '')} at {e.get('company', '')}" for e in experience[:3]
        )

        prompt = (
            "Rewrite this professional resume summary to naturally include the target role "
            "and key skills. The summary belongs on the candidate's own resume, so it must "
            "be written FROM the candidate's perspective (first-person-implied, no 'I' "
            "pronoun) — never AS an outside observer describing the candidate.\n\n"
            "VOICE — non-negotiable:\n"
            "  • Write as the candidate's own self-description. Opening should describe what\n"
            "    the candidate IS or DOES, e.g. 'Backend engineer with hands-on experience\n"
            "    building...' / 'Cloud and DevOps engineer focused on...'.\n"
            "  • NEVER use the words 'candidate', 'candidates', 'the candidate',\n"
            "    'This candidate', 'Software Engineer X candidates should...', or any\n"
            "    third-person framing. This is a resume summary, not a job description or\n"
            "    a recruiter blurb.\n"
            "  • NEVER open the summary with the JD's job title as a noun phrase being\n"
            "    described (e.g. 'Software Engineer II Backend candidates should have...').\n"
            "    The JD title can appear LATER in the summary as the candidate's target\n"
            "    role, but not as the grammatical subject of the opening sentence.\n"
            "\n"
            "STYLE rules:\n"
            "1. EXACTLY 3-4 sentences. Not 5, not 6.\n"
            "2. Naturally mention the target role title and at least 3 of the required skills.\n"
            "3. Ground every claim in the candidate's actual experience below.\n"
            "4. Use plain, professional language. Direct, specific, no buzzwords.\n"
            "5. BANNED WORDS — do NOT use ANY: 'Versatile', 'Proficient', 'Leverages',\n"
            "   'extensive experience', 'proven track record', 'results-driven', 'passionate',\n"
            "   'detail-oriented', 'highly skilled', 'seasoned', 'cutting-edge', 'innovative',\n"
            "   'dynamic', 'self-motivated', 'Adept', 'dedicated', 'committed to excellence',\n"
            "   'strong foundation', 'strong record', 'strong background', 'robust experience',\n"
            "   'should have', 'candidates should'.\n"
            "6. Do NOT start with an adjective.\n"
            "7. Do NOT start with 'I' or with the JD's job title verbatim.\n\n"
            "GOOD examples (match this voice):\n"
            "  • 'Backend engineer with hands-on experience building Python microservices on\n"
            "    AWS, designing RESTful APIs against PostgreSQL, and shipping CI/CD pipelines\n"
            "    that span dev through production. Comfortable owning the full development\n"
            "    lifecycle in fast-paced environments. Ready to apply this foundation to the\n"
            "    Software Engineer II role on the Platform Team.'\n"
            "  • 'Cloud and DevOps engineer skilled in Terraform-driven AWS infrastructure,\n"
            "    Kubernetes workloads, and golden-signal observability across production\n"
            "    systems. Background spans containerized backend services on ECS/EKS with\n"
            "    Multi-AZ Postgres and SRE-aligned incident response.'\n\n"
            f"Target role: {jd_analysis.get('job_title', '')}\n"
            f"Required skills: {', '.join(jd_analysis.get('required_skills', [])[:5])}\n"
            f"Candidate background: {exp_context}\n"
            f"Current summary: {summary}\n\n"
            "Return ONLY the rewritten summary as a JSON object with one key named "
            "\"summary\" whose value is the new summary string. The string value must be "
            "plain prose. Just the prose.\n"
        )

        try:
            # NOTE: do NOT pass schema= here. Earlier we used schema={"summary": str}
            # which triggered Claude's tool-use mode; combined with the prompt's
            # JSON example, Claude returned `{"summary": "{\"summary\": \"...\"}"}`
            # — a nested envelope — which got stored verbatim as the summary string
            # and showed in the rendered PDF as `{"summary": "I build..."}`. Plain
            # prompt + JSON parse + envelope unwrap below is the safe path.
            result = gemini_json(
                prompt, max_tokens=6000, temperature=0.3,
                model=GEMINI_FLASH,
            )
            new_summary = result.get("summary", "") if isinstance(result, dict) else ""
            # Defensive unwrap: if the model still nests {"summary": "..."} inside
            # the value, peel one layer.
            if isinstance(new_summary, str) and new_summary.strip().startswith("{"):
                try:
                    import json as _json
                    parsed = _json.loads(new_summary)
                    if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str):
                        new_summary = parsed["summary"]
                        logger.warning("ContentAugmenter ATS: peeled nested {summary:...} envelope")
                except Exception:
                    pass
            if new_summary and len(new_summary) > 50:
                # Defensive QC: reject the rewrite if the model slipped back
                # into the third-person voice or opened with the JD title
                # verbatim. Keep the original (tailor-produced) summary in
                # that case — it's almost always better than a broken
                # rewrite. The list below captures every red-flag pattern
                # we've seen the model emit in production.
                _new_lower = new_summary.lower()
                _job_title = (jd_analysis.get("job_title") or "").strip().lower()
                _bad_patterns = [
                    " candidate ", " candidates ", "the candidate", "this candidate",
                    "candidates should", "should have built",
                    "i build ", "i have built ", "i'm a ", "i am a ",
                ]
                _opens_with_jd_title = (
                    _job_title
                    and len(_job_title) > 6
                    and _new_lower.startswith(_job_title)
                )
                if any(p in _new_lower for p in _bad_patterns) or _opens_with_jd_title:
                    logger.warning(
                        "ContentAugmenter ATS: REJECTED alignment rewrite "
                        "(third-person voice or JD-title opener) — keeping original. Rewrite was: %r",
                        new_summary[:200],
                    )
                else:
                    tailored["summary"] = new_summary
                    logger.warning("ContentAugmenter ATS: summary rewritten for alignment")
        except Exception as e:
            logger.error("ContentAugmenter ATS summary alignment failed: %s", e)

        return tailored

    @staticmethod
    def _enforce_chronological_order(tailored: Dict[str, Any]) -> Dict[str, Any]:
        """Sort experience entries in reverse-chronological order (most recent first)."""
        from services.resume_parser import _PRESENT_KEYWORDS, _YEAR_PATTERN

        experience = tailored.get("experience", [])
        if len(experience) <= 1:
            return tailored

        def _sort_key(exp: dict) -> tuple:
            dates = exp.get("dates", "").lower()
            # Present/current → sort to top (year 9999)
            if any(kw in dates for kw in _PRESENT_KEYWORDS):
                return (9999, 12)
            years = _YEAR_PATTERN.findall(dates)
            if years:
                end_year = int(years[-1])
                return (end_year, 6)  # Default to mid-year
            return (0, 0)  # No date → sort to bottom

        experience.sort(key=_sort_key, reverse=True)
        tailored["experience"] = experience
        return tailored


# ------------------------------------------------------------------
# Helpers (module-level)
# ------------------------------------------------------------------

def _get_entry_context(entry: dict) -> str:
    """Get a short context string for a resume entry."""
    company = entry.get("company", "")
    title = entry.get("title", "")
    name = entry.get("name", "")
    if company and title:
        return f"{title} at {company}"
    if name:
        return f"Project: {name}"
    return "Unknown"
