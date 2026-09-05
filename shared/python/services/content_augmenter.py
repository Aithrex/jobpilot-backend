"""
Content Augmenter — fills sparse resumes to ~90% page fill with projects, bullets, and impact.

Pipeline integration: runs AFTER tailor + integrity guard, BEFORE date normalization and complete_job.
Subsumes the old single-project injection logic.

Phases:
  0: Measure fill → early exit if >= 90% (except project generation, which always runs if < 3)
  1: Generate projects (batch: up to 3 total in ONE LLM call)
  2: Expand experience bullets + baked-in impact metrics
  3: Impact injection on existing no-metric bullets
  3b: Second expansion round against the stretch caps if still well short
  4: Overflow protection (trim if > 95%)
  5: ATS hardening (keyword density, then weak verbs)
  6: Final overflow check after hardening
"""
import copy
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Import METRIC_PATTERN from impact_engine for consistent metric detection
from services.impact_engine import METRIC_PATTERN, WEAK_VERB_PHRASES

# Shared prompt-rule kernel (Part D) — every bullet-writing LLM call here
# interpolates these so it cooperates with the guards instead of emitting tokens
# the guard later strips/penalizes.
from services import prompt_rules

# Target fill thresholds
_TARGET_FILL = 0.90
_OVERFLOW_SOFT = 0.95
_MAX_PROJECTS = 3
_MAX_TOTAL_EXP_BULLETS = 20

# Per-role bullet caps by recency (index 0 = most recent)
_ROLE_BULLET_CAPS = [8, 6, 4, 4, 3]  # roles beyond index 4 get cap of 3

# Second expansion round. The caps above are deliberately conservative so a
# resume that reaches the target on real content stops growing. When the page
# is STILL well short after impact injection, those caps — not the candidate's
# material — are what's binding, and the renderer would otherwise be handed a
# half-empty page to lay out. One more grounded round runs against these.
_SECOND_ROUND_FILL = 0.75
_STRETCH_ROLE_BULLET_CAPS = [10, 8, 6, 5, 4]
_STRETCH_MAX_TOTAL_EXP_BULLETS = 26


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

        The tailor pipeline runs IntegrityGuard after the primary LLM emit,
        and summary_tailor runs cosmetics after the deferred summary is
        accepted. The augmenter's Phase 2 (bullet expansion), Phase 3
        (impact injection), and Phase 5 (keyword density / weak verbs)
        all call the LLM again to rewrite or add bullets.
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
        preserve_project_names: set | None = None,
        allow_project_generation: bool = True,
    ) -> Dict[str, Any]:
        """Run all augmentation phases on a tailored resume.

        Args:
            tailored: The tailored resume dict (modified in-place).
            original: The original parsed resume (for grounding).
            jd_analysis: JD analysis dict with required_skills, keywords, etc.
            preserve_project_names: allowlist for the defensive project
                strip — names of legitimately GENERATED projects (parallel
                pre-injection in extract_and_tailor, GitHub-grounded
                projects) that aren't in the original resume but must
                survive. Without this, the strip silently discarded
                pre-injected projects and forced Phase 1 to regenerate them.
            allow_project_generation: experience-first policy gate (see
                project_generator.project_generation_policy). False for
                experienced candidates — Phase 1 is skipped and the page
                budget goes to their real experience bullets instead.

        Returns:
            The augmented tailored resume dict.
        """
        from services.tailoring_pipeline import is_result
        if is_result(tailored):
            # Frozen new-pipeline content must never enter the legacy semantic
            # writers, even when a legacy adapter still calls augment().
            return tailored
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
            # Fill trace — without this, a resume that ships at 55% full looks
            # identical in the logs to one that legitimately reached target.
            "fill_target": _TARGET_FILL,
            "fill_phase0": None,
            "fill_final": None,
            "phase_gated": None,
            "second_round": False,
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
        # ProjectGenerator can do its job. Legitimately generated projects
        # (parallel pre-injection, GitHub-grounded) survive via the
        # preserve_project_names allowlist.
        _orig_proj_keys = {
            (p.get("name", "") or "").lower().strip()
            for p in original.get("projects", [])
        }
        _orig_proj_keys |= {
            (n or "").lower().strip() for n in (preserve_project_names or set())
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
        diag["fill_phase0"] = round(fill, 4)
        _proj_names = [p.get("name", "?") for p in tailored.get("projects", [])]
        _orig_proj_names = [p.get("name", "?") for p in original.get("projects", [])]
        logger.warning(
            "ContentAugmenter Phase 0: fill=%.1f%%, projects=%d %s, original_projects=%d %s, experience=%d",
            fill * 100,
            len(_proj_names), _proj_names,
            len(_orig_proj_names), _orig_proj_names,
            len(tailored.get("experience", [])),
        )

        # Phase 1: project generation — gated by the experience-first policy.
        # Early-career resumes are expected to carry projects; experienced
        # resumes get their page budget spent on real experience instead
        # (generated filler projects read as padding to recruiters and evict
        # experience bullets in the one-page allocation).
        projects = tailored.get("projects", [])
        if not allow_project_generation:
            logger.warning(
                "ContentAugmenter Phase 1 SKIPPED by experience-first policy — "
                "page budget goes to experience bullets"
            )
            diag["deep_project_skip"] = True
            diag["phase_gated"] = "projects_experience_first_policy"
        elif len(projects) < _MAX_PROJECTS:
            t0 = time.time()
            tailored = self._augment_projects(tailored, original, jd_analysis)
            fill = self._measure_fill(tailored)
            logger.warning("ContentAugmenter Phase 1 (projects): fill = %.1f%% [%.1fs]",
                           fill * 100, time.time() - t0)
        else:
            logger.warning("ContentAugmenter Phase 1 SKIPPED: tailor returned %d projects (cap %d) — "
                           "no room for generated projects. This is usually a tailor-prompt issue.",
                           len(projects), _MAX_PROJECTS)
            diag["phase_gated"] = "projects_at_cap"

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
            diag["fill_final"] = round(fill, 4)
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

        # Phase 3b: second expansion round. Everything above respects the
        # conservative per-role caps; if the page is still well short, those
        # caps are the binding constraint rather than the candidate's material.
        if fill < _SECOND_ROUND_FILL and tailored.get("experience"):
            t0 = time.time()
            tailored = self._expand_bullets(
                tailored, original, jd_analysis, fill,
                role_caps=_STRETCH_ROLE_BULLET_CAPS,
                max_total=_STRETCH_MAX_TOTAL_EXP_BULLETS,
            )
            fill = self._measure_fill(tailored)
            diag["second_round"] = True
            logger.warning(
                "ContentAugmenter Phase 3b (second expansion): fill = %.1f%% [%.1fs]",
                fill * 100, time.time() - t0,
            )

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

        diag["fill_final"] = round(fill, 4)
        logger.info("ContentAugmenter: final fill = %.1f%%, total pipeline time = %.1fs",
                    fill * 100, time.time() - pipeline_start)
        if fill < _TARGET_FILL:
            # Not an error — some candidates genuinely have half a page of
            # material. Worth surfacing, because the renderer will now lay it
            # out compactly instead of stretching it to look full.
            logger.warning(
                "ContentAugmenter: final fill %.1f%% under the %.0f%% target "
                "(phase_gated=%s, second_round=%s, roles=%d, projects=%d)",
                fill * 100, _TARGET_FILL * 100,
                diag.get("phase_gated"), diag.get("second_round"),
                len(tailored.get("experience", []) or []),
                len(tailored.get("projects", []) or []),
            )
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
        role_caps: List[int] | None = None,
        max_total: int | None = None,
    ) -> Dict[str, Any]:
        """Expand experience bullets to fill the page.

        `role_caps` / `max_total` default to the conservative first-round
        limits; the Phase 3b retry passes the stretch caps instead.
        """
        role_caps = role_caps or _ROLE_BULLET_CAPS
        max_total = max_total or _MAX_TOTAL_EXP_BULLETS
        from services.claude_client import claude_json, CLAUDE_FLASH
        from schemas.resume_schemas import build_resume_text
        import json

        experience = tailored.get("experience", [])
        if not experience:
            return tailored

        # Calculate total current bullets
        total_current = sum(len(exp.get("bullets", [])) for exp in experience)
        if total_current >= max_total:
            return tailored

        # Calculate target bullets per role
        expansion_plan = []
        for i, exp in enumerate(experience):
            current = len(exp.get("bullets", []))
            cap = role_caps[i] if i < len(role_caps) else 3

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
        budget = max_total - total_current
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
            f"1. {prompt_rules.experience_bullet_length_rule()}\n"
            "2. Each bullet follows: Action Verb + Technology/Context + a concrete, "
            "interview-defensible outcome (the candidate could expand it into a full "
            "Situation-Task-Action-Result story).\n"
            f"3. {prompt_rules.METRIC_HONESTY_RULE}\n"
            f"4. {prompt_rules.BANNED_SYMBOLS_RULE}\n"
            f"5. {prompt_rules.VERB_DIVERSITY_RULE}\n"
            f"6. {prompt_rules.TERMINAL_PUNCTUATION_RULE}\n"
            f"7. {prompt_rules.VOICE_RULE}\n"
            "8. Ground every bullet in the candidate's ACTUAL skills and experience "
            "context below. Do NOT fabricate companies, clients, or production systems, "
            "and do NOT duplicate content from existing bullets.\n"
            f"9. Naturally incorporate these JD keywords where relevant (do not stuff — "
            f"aim for {prompt_rules.KEYWORD_COVERAGE_LOW}-{prompt_rules.KEYWORD_COVERAGE_HIGH}% "
            f"coverage, not 100%): {required_skills}\n"
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
            result = claude_json(
                prompt, max_tokens=12000, temperature=0.4,
                model=CLAUDE_FLASH, schema=EXPANSION_SCHEMA,
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
        from services.claude_client import claude_json, CLAUDE_FLASH

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
            "You are sharpening resume bullets that currently read as flat activity, "
            "making each a concrete, interview-defensible accomplishment.\n\n"
            "STRICT RULES:\n"
            f"1. {prompt_rules.METRIC_HONESTY_RULE}\n"
            "   Do NOT invent a number to satisfy this. If the action has no grounded "
            "number, add a concrete OUTCOME clause instead (what changed as a result), "
            "not a fabricated metric.\n"
            f"   Context signals (for judging what scope is plausible, NOT numbers to "
            f"insert): {context_signals}\n"
            f"2. {prompt_rules.BANNED_SYMBOLS_RULE}\n"
            "3. Derive the outcome from the ACTION described — don't attach unrelated "
            "claims. Name what the work served, enabled, removed, or eliminated. "
            "(e.g. built a service → what it served; ran a campaign → what it lifted; "
            "closed the books → what it reduced; managed patients → the care outcome).\n"
            "4. Dollar figures are allowed ONLY when the source already states them "
            "(revenue, budget, savings are the core metric in finance/sales — keep a "
            "grounded number, never invent one).\n"
            "5. Keep the original meaning intact — only ADD an outcome/metric clause, "
            "don't rewrite the action.\n"
            f"6. {prompt_rules.TERMINAL_PUNCTUATION_RULE}\n"
            f"7. Keep each enhanced bullet within {prompt_rules.EXPERIENCE_BULLET_MAX_CHARS} "
            "characters.\n\n"
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
            result = claude_json(
                prompt, max_tokens=12000, temperature=0.3,
                model=CLAUDE_FLASH, schema=IMPACT_SCHEMA,
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

        # Content selection owns page-budget bullet choices. Do not silently
        # pop project/experience bullets here — that caused preview/export drift
        # and discarded uniquely covering third project bullets.
        if fill_at_9pt > 1.0:
            logger.warning(
                "ContentAugmenter Phase 4: still overflowing at 9pt (%.1f%%) — "
                "deferring to content_selection / two-page layout (no silent trim).",
                fill_at_9pt * 100,
            )
        return tailored

    def _overflow_trim_legacy_disabled(self, tailored: Dict[str, Any], fill: float) -> Dict[str, Any]:
        """Retained for reference; silent first-N truncation removed."""
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

        Keyword density rewrites under-evidenced bullets; weak verbs run
        after that (they depend on the density output). The professional
        summary is written earlier by summary_tailor and is not touched here.
        """
        try:
            tailored = self._enforce_keyword_density(tailored, jd_analysis)
        except Exception as e:
            logger.error("ContentAugmenter ATS: keyword density failed: %s", e)

        tailored = self._fix_weak_verbs(tailored)
        tailored = self._enforce_chronological_order(tailored)
        return tailored

    def _enforce_keyword_density(
        self,
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Strengthen how PRESENT keywords are evidenced in bullets — never
        invent absent ones.

        Coverage is measured on summary + experience/project bullets (the ATS
        "used in context" zones), NOT the skills list. A skills dump alone
        used to trip the >=80% skip and leave bullets untouched, which tanks
        hard_skill_match after the deferred-summary path.

        Anti-stuffing alignment with the tailor prompt's 80-90% coverage
        target:
          - Keywords with ZERO occurrences anywhere stay the tailor's call —
            this pass does not invent them.
          - When required-keyword coverage in bullets/summary is already
            >= 80%, skip entirely.
          - Keywords appearing exactly ONCE in context, OR present only in
            the skills list (0 in context), get woven into one more bullet
            as usage context, capped at 8.
        """
        from services.claude_client import claude_json, CLAUDE_FLASH
        from schemas.resume_schemas import build_resume_text
        from utils.keyword_normalizer import get_all_forms

        required = jd_analysis.get("required_skills", [])
        if not required:
            return tailored

        def _count_in(text: str, skill: str) -> int:
            total = 0
            for form in get_all_forms(skill):
                if len(form) <= 2:
                    total += text.count(form)
                else:
                    pattern = re.compile(
                        r"(?<![a-zA-Z])" + re.escape(form) + r"(?![a-zA-Z])"
                    )
                    total += len(pattern.findall(text))
            return total

        # Same zones the honest scorer uses for "used, not just listed".
        context_parts: List[str] = [tailored.get("summary") or ""]
        for exp in tailored.get("experience") or []:
            context_parts.extend(
                b for b in (exp.get("bullets") or []) if isinstance(b, str)
            )
        for proj in tailored.get("projects") or []:
            context_parts.extend(
                b for b in (proj.get("bullets") or []) if isinstance(b, str)
            )
        context_text = "\n".join(p for p in context_parts if p).lower()
        full_text = build_resume_text(tailored).lower()

        context_counts: Dict[str, int] = {}
        full_counts: Dict[str, int] = {}
        for skill in required:
            context_counts[skill] = _count_in(context_text, skill)
            full_counts[skill] = _count_in(full_text, skill)

        present_in_context = sum(1 for c in context_counts.values() if c >= 1)
        required_coverage = present_in_context / len(required)
        if required_coverage >= 0.8:
            logger.info(
                "ContentAugmenter ATS: required coverage %.0f%% already in "
                "bullets/summary — skipping keyword hardening (anti-stuffing)",
                required_coverage * 100,
            )
            return tailored

        # Once in context → strengthen. In skills only → weave into a bullet.
        # Nowhere in the resume → never invent.
        under_represented = [
            s for s in required
            if context_counts[s] == 1
            or (context_counts[s] == 0 and full_counts[s] >= 1)
        ][:8]
        if not under_represented:
            logger.info(
                "ContentAugmenter ATS: no single-occurrence or skills-only "
                "keywords to strengthen (absent keywords are never injected)"
            )
            return tailored

        logger.info(
            "ContentAugmenter ATS: strengthening %d keywords into bullets: %s",
            len(under_represented), under_represented,
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
            "You are strengthening how existing resume keywords are evidenced — NOT adding "
            "new claims.\n\n"
            "RULES:\n"
            "1. Each keyword below already appears somewhere on this resume (often only in\n"
            "   Skills). Weave it naturally into ONE relevant bullet as usage context (what\n"
            "   was done with it) — do NOT just append it, and do NOT leave the bullet unchanged.\n"
            "2. Preserve the original meaning, structure, and EVERY NUMBER in each bullet.\n"
            "3. NEVER invent facts, metrics, tools, or named products beyond the keywords listed —\n"
            "   you may only rephrase what the bullet already says so the keyword shows up in use.\n"
            "4. Where it fits naturally, shape the bullet as 'Accomplished X, as measured by Y,\n"
            "   by doing Z' — using only numbers already present in the bullet.\n"
            f"5. {prompt_rules.BANNED_SYMBOLS_RULE}\n"
            f"6. {prompt_rules.TERMINAL_PUNCTUATION_RULE}\n"
            f"7. Keep each bullet within {prompt_rules.EXPERIENCE_BULLET_MAX_CHARS} characters.\n"
            "8. Only modify bullets where the keyword fits naturally; skip any that don't.\n\n"
            f"Keywords to strengthen: {', '.join(under_represented)}\n\n"
            f"Current bullets:\n{bullets_text}\n\n"
            "Return a JSON object:\n"
            '{"modified_bullets": [\n'
            '  {"index": 1, "text": "The modified bullet with keyword integrated"}\n'
            "]}\n"
            "Only include bullets you actually changed.\n"
        )

        KEYWORD_SCHEMA = {"modified_bullets": [{"index": int, "text": str}]}

        try:
            result = claude_json(
                prompt, max_tokens=24000, temperature=0.3,
                model=CLAUDE_FLASH, schema=KEYWORD_SCHEMA,
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
        from services.claude_client import claude_json, CLAUDE_FLASH

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
            f"2. {prompt_rules.VERB_DIVERSITY_RULE}\n"
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
            result = claude_json(
                prompt, max_tokens=12000, temperature=0.2,
                model=CLAUDE_FLASH, schema=VERB_SCHEMA,
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
