"""Canonical finalization for every tailored resume, whatever produced it.

Before this module, each entry point assembled its own post-tailor sequence.
Single tailor, regenerate and guest ran augment -> normalize -> verify; batch
hand-rolled a shorter version that skipped claim auditing and entailment
verification entirely; auto-apply prepare skipped augmentation as well and
then pinned a PDF from the unverified result. Same product, three different
safety floors, and the weakest one fed the artifact that gets submitted.

Everything now goes through `finalize()`. It owns the ordering, the guards,
the cancellation checkpoints and the provenance envelope, so adding a stage
means editing one function rather than auditing every caller.

Ordering matters, and one detail is load-bearing: the augmenter makes its own
LLM calls, so `IntegrityGuard` running inside `ResumeTailor.tailor()` cannot
see anything the augmenter later writes. `finalize()` therefore re-runs the
FULL guard AFTER augmentation. The upstream call stays where it is — it gives
the tailor a clean draft to reason about — but the enforcement floor for what
reaches a renderer is the pass here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# Bump when the FINALIZATION sequence changes (stage order, guard strength,
# what counts as blocking). Stored on the result so a stale score or
# verification sidecar can be told apart from one produced by current code.
# Distinct from ResumeRenderer.RENDER_VERSION, which versions page layout.
PIPELINE_VERSION = "finalize-2026-09-01.3"


class JobCancelled(Exception):
    """Raised at a checkpoint when the user cancelled the job.

    Carries no message the user will see: `_process_job` catches it and stops
    without calling `fail_job`, because `cancel_job` already made the row
    terminal and refunded the reservation.
    """

    def __init__(self, job_id: str = ""):
        self.job_id = job_id
        super().__init__(f"job {job_id} cancelled")


def check_cancelled(job_id: str) -> None:
    """Stop the pipeline if the job was cancelled while we were working.

    Cooperative by necessity: async jobs run as a Lambda `Event` invoke or a
    local daemon thread, and neither can be killed from outside, so the worker
    has to notice and stop itself. Called at stage boundaries only — one small
    indexed read against a Mongo document that is already hot, next to an LLM
    call that costs seconds.

    Fails open. If the status read errors we continue working: wrongly
    finishing a cancelled job wastes tokens, wrongly aborting a live one
    destroys the user's result.
    """
    if not job_id:
        return
    try:
        from utils.db_connect import DBConnect

        row = DBConnect().get_db()["resume_jobs"].find_one(
            {"job_id": job_id}, {"status": 1, "cancel_requested": 1}
        )
        if not row:
            return
        if row.get("cancel_requested") or row.get("status") == "cancelled":
            logger.info("Job %s cancelled — stopping at checkpoint", job_id)
            raise JobCancelled(job_id)
    except JobCancelled:
        raise
    except Exception as e:
        logger.warning("Cancel check failed for %s (continuing): %s", job_id, e)


def attach_ats_keyword_audit(result: Dict[str, Any], jd_analysis: Dict[str, Any]) -> None:
    """Re-run the deterministic keyword gap engine on the FINAL tailored
    resume so the UI can warn about JD keywords that didn't make it in.

    The LLM occasionally paraphrases tokens (e.g. "Amazon Web Services" in
    place of the literal "AWS" the JD asked for); enterprise ATS screeners
    match exact strings. Attached as `ats_keyword_audit` for a passive badge,
    never blocking. Failure here must not break the tailor pipeline.
    """
    try:
        from services.keyword_gap_engine import KeywordGapEngine
        audit = KeywordGapEngine().analyze(result, jd_analysis or {})
        result["ats_keyword_audit"] = {
            "matched": audit.get("matched_keywords", []),
            "missing": audit.get("missing_keywords", []),
            "partial": audit.get("partial_matches", []),
            "coverage_percentage": audit.get("coverage_percentage", 0),
            "required_missing": audit.get("required_missing", []),
            "required_coverage": audit.get("required_coverage", 0),
        }
        missed = result["ats_keyword_audit"]["required_missing"]
        if missed:
            logger.warning(
                "ATS audit: %d required JD keyword(s) NOT in tailored resume — %s",
                len(missed), missed[:8],
            )
    except Exception as e:
        logger.warning("ATS audit skipped: %s", e)


def consume_projects_diagnostic(result: Dict[str, Any]) -> None:
    """Pop the augmenter's internal diagnostic, log a structured summary, and
    re-attach it under `_diagnostics.projects` so the job status response can
    explain why a Projects section ended up empty.

    Removed from the top level so the renderer never sees it.
    """
    diag = result.pop("_projects_diagnostic", None)
    if not diag:
        return
    rejections = diag.get("rejections", []) or []
    logger.warning(
        "PROJECTS DIAGNOSTIC: stage=%s final_count=%d budget=%s "
        "generator_returned=%d rejections=%d defensive_stripped=%s deep_skip=%s "
        "fill=%s->%s (target %s) phase_gated=%s second_round=%s",
        diag.get("stage_reached", "unknown"),
        diag.get("final_count", 0),
        diag.get("budget_shape"),
        diag.get("generator_returned", 0),
        len(rejections),
        diag.get("defensive_stripped", []),
        diag.get("deep_project_skip", False),
        diag.get("fill_phase0"), diag.get("fill_final"), diag.get("fill_target"),
        diag.get("phase_gated"), diag.get("second_round"),
    )
    if rejections:
        for r in rejections[:10]:
            logger.warning("  rejection: %s -> %s", r.get("name"), r.get("reason"))
    result.setdefault("_diagnostics", {})["projects"] = diag


def _normalize_contact(result: Dict[str, Any]) -> None:
    contact = result.get("contact") or {}
    if contact.get("name"):
        from services.resume_parser import ResumeParser as _RP
        contact["name"] = _RP._normalize_name(" ".join(contact["name"].split()))
    if contact.get("phone"):
        contact["phone"] = " ".join(contact["phone"].split())


def _enforce_integrity(
    original: Dict[str, Any],
    result: Dict[str, Any],
    jd_analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """Re-run the deterministic integrity floor on post-augmentation content.

    The augmenter can add bullets, inject outcome clauses and rewrite verbs
    through its own LLM calls, none of which the guard inside `tailor()` ever
    saw. Fails open on error: an integrity crash must not lose the user's
    resume, and the claim audit below is a second net.
    """
    try:
        from services.integrity_guard import IntegrityGuard

        corrected, report = IntegrityGuard().enforce(original, result, jd_analysis)
        logger.info(
            "Post-augment integrity: severity=%s hallucinated_exp=%d "
            "hallucinated_proj=%d certs_stripped=%d",
            report.severity,
            len(report.hallucinated_experience),
            len(report.hallucinated_projects),
            len(report.fabricated_certs_stripped),
        )
        return corrected
    except Exception as e:
        logger.warning("Post-augment integrity pass failed (keeping content): %s", e)
        return result


def _attach_verification(
    result: Dict[str, Any],
    structured: Dict[str, Any],
    jd_analysis: Dict[str, Any],
    user_email: str = "",
    fact_store: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Claim-guard audit + LLM entailment pass on the FINAL tailored text.

    `audit_resume` mutates `result` in place when it can safely downgrade an
    ungrounded metric, so the corrected content is what gets verified,
    rendered and stored — the audit is not advisory about its own edits.

    The entailment verifier on top of it IS advisory and fails open: any
    failure degrades to `status: "skipped"` rather than breaking the job. The
    deterministic guards above are the enforcement floor.
    """
    try:
        from services.claim_guard import audit_resume
        from services.profile_facts import build_fact_store, grounding_texts
        from services.tailor_verifier import verify

        if fact_store is None:
            fact_store = build_fact_store(
                user_email=user_email, structured_resume=structured
            )
        corrected, audit = audit_resume(
            result,
            structured,
            jd_analysis,
            extra_grounding_texts=grounding_texts(fact_store),
        )
        # Same object as `result` (in-place write-back), but assign through so
        # the correction is visibly kept rather than looking discarded.
        result.update(corrected)
        if audit.downgraded:
            logger.info(
                "claim_guard downgraded %d ungrounded metric(s) before render",
                audit.downgraded,
            )
        return verify(result, fact_store, claim_audit=audit)
    except Exception as e:
        logger.warning("Verification pass failed (fails open): %s", e)
        return {
            "status": "skipped",
            "reason": str(e),
            "model": "flash",
            "counts": {"supported": 0, "partial": 0, "unsupported": 0, "unverified": 0},
            "items": {},
        }


def blocking_claims(result: Dict[str, Any]) -> list:
    """Addresses whose claims must be resolved before the resume may download.

    High severity means the deterministic guard found a number with no source
    anywhere in the candidate's evidence. `unsupported_product` and the
    verifier's own verdicts are deliberately excluded: naming an adjacent tool
    is a judgment call for the candidate, and the entailment model is not
    reliable enough to hard-block on.
    """
    items = ((result.get("verification") or {}).get("items") or {})
    blocking = []
    for address, item in items.items():
        flags = item.get("flags") or []
        if "ungrounded_metric" in flags:
            blocking.append({
                "address": address,
                "text": item.get("text", ""),
                "flags": flags,
                "unsupported_claims": item.get("unsupported_claims", []),
            })
    return blocking


def stamp_provenance(result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the finalization envelope: what produced this content.

    `content_hash` is the same canonical hash the version store and the S3
    render cache use, so a score, a verification sidecar and a downloaded file
    can all be checked against one identifier.
    """
    from services.resume_renderer import ResumeRenderer
    from services.resume_versioning import canonical_content_hash

    blocking = blocking_claims(result)
    result["finalization"] = {
        "pipeline_version": PIPELINE_VERSION,
        "render_version": ResumeRenderer.RENDER_VERSION,
        "content_hash": canonical_content_hash(
            {k: v for k, v in result.items() if not k.startswith("_")
             and k not in ("finalization", "readiness", "layout_plan")}
        ),
        "blocking_claim_count": len(blocking),
        "blocking_claims": blocking,
    }
    return result


def finalize(
    svc,
    job_id: str,
    result: Dict[str, Any],
    structured: Dict[str, Any],
    jd_analysis: Dict[str, Any],
    user_email: str = "",
    fact_store: Optional[Dict[str, Any]] = None,
    preserve_project_names: Optional[Set[str]] = None,
    allow_project_generation: bool = True,
) -> Dict[str, Any]:
    """The one post-tailor pipeline. Every producer of a tailored resume calls
    this and nothing else.

    Stages, in order, with a cancellation checkpoint before each expensive one:
      contact normalization -> augment -> date/title normalization ->
      full integrity enforcement -> claim audit + entailment verification ->
      keyword audit -> provenance envelope

    Raises `JobCancelled` if the user cancelled; callers let it propagate to
    `_process_job`, which stops without failing the (already terminal) row.
    """
    _normalize_contact(result)

    from utils.feature_flags import enabled as flag_on

    check_cancelled(job_id)
    content_plan = None
    if flag_on("PLAN_GUIDED_WRITERS"):
        try:
            from services.analysis_wave import run_wave_a
            from services.content_planner import plan as build_plan

            fact_store, jd_analysis = run_wave_a(
                user_email, structured, "", jd_analysis or {}, fact_store
            )
            content_plan = build_plan(structured or result, jd_analysis, fact_store)
            if content_plan.get("allow_project_generation") is False:
                allow_project_generation = False
        except Exception as e:
            logger.warning("content plan skipped: %s", e)

    skip_augmenter = (
        flag_on("PLAN_WRITERS_REPLACE_AUGMENTER")
        or (content_plan is not None and not content_plan.get("allow_project_generation"))
    )

    check_cancelled(job_id)
    if job_id:
        svc.update_job_progress(job_id, "augmenting", 60)
    if skip_augmenter:
        logger.info("finalize: skipping ContentAugmenter (plan-guided writers own this draft)")
    else:
        result = svc.augmenter.augment(
            result, structured, jd_analysis,
            preserve_project_names=preserve_project_names,
            allow_project_generation=allow_project_generation,
        )
        consume_projects_diagnostic(result)

    if flag_on("PLAN_GUIDED_WRITERS") and content_plan is not None:
        check_cancelled(job_id)
        try:
            from services.plan_writers import apply_plan
            result = apply_plan(result, structured, content_plan, fact_store)
        except Exception as e:
            logger.warning("plan writers skipped: %s", e)

    from services.date_normalizer import normalize_dates
    from services.title_normalizer import normalize_titles

    result = normalize_dates(result)
    result = normalize_titles(result)

    # The augmenter's LLM passes ran after the tailor's own guard, so this is
    # the pass that actually decides what may reach a renderer.
    check_cancelled(job_id)
    result = _enforce_integrity(structured, result, jd_analysis)

    check_cancelled(job_id)
    if job_id:
        svc.update_job_progress(job_id, "verifying", 80)
    result["verification"] = _attach_verification(
        result, structured, jd_analysis, user_email, fact_store=fact_store
    )

    if job_id:
        svc.update_job_progress(job_id, "finalizing", 95)
    attach_ats_keyword_audit(result, jd_analysis)
    result["_page_format"] = _page_format_for(jd_analysis)
    try:
        from services.readiness_service import analyze as analyze_readiness
        result["readiness"] = analyze_readiness(result, jd_analysis)
    except Exception as e:
        logger.warning("readiness sidecar skipped: %s", e)
    try:
        result["layout_plan"] = svc.renderer.layout_plan(result)
    except Exception as e:
        logger.warning("layout_plan skipped: %s", e)
    return stamp_provenance(result)


def _page_format_for(jd_analysis: Dict[str, Any]) -> str:
    """US Letter for US applications, A4 otherwise."""
    loc = str((jd_analysis or {}).get("location") or "").lower()
    if any(tok in loc for tok in (
        "united states", "u.s.a", "u.s.", "usa",
    )) or loc.endswith(", us") or loc.endswith(" us") or ", us," in loc:
        return "Letter"
    return "A4"
