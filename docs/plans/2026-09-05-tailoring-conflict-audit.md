# Tailoring conflict audit

Status: static review of the local working tree, 2026-09-05. No runtime changes or live generation tests performed by this audit. Function references identify evidence without depending on moving line numbers.

Resolution: [proposed unified architecture](../specs/evidence-led-tailoring-architecture.md).

## Changes observed during review

The summary workstream was updated concurrently. Current `summary_tailor.py` now ranks JD evidence, distinguishes broad SWE positioning, selects optional capabilities/domain, reports `summary_write`, and exposes `refresh_summary_if_stale`. Shared service finalization invokes the refresh. These are improvements already present locally, not outstanding implementation tasks. Their deployed status and live quality were not verified. Earlier summary findings in the product plan are historical and must not be treated as the current implementation.

## Remaining conflicts

| ID | Source and observed behavior | Resolution required |
|---|---|---|
| C01 | `prompt_rules` sets coverage at 75–80%; `ResumeTailor._build_keyword_context` asks for 80–90%; augmentation skips at 80%. | Remove arbitrary writing quotas. Report evidence coverage; do not reward duplicate terms. |
| C02 | `_build_keyword_context` asks missing terms to be woven into adjacent work; its HTTP-service example assumes REST APIs. | Distinguish related experience from equivalent experience. A JD is never candidate evidence. |
| C03 | `_ensure_jd_skills_coverage` accepts evidence from original plus tailored text; `KeywordGapEngine` promotes implied skills to matched. | Freeze source facts. Generated text cannot justify more generated claims. Keep listed/related/demonstrated separate. |
| C04 | Skill restoration and `IntegrityGuard` certification mirroring can undo selective display. | Separate factual immutability from authorized selection. Preserve values of retained claims and the full source history. |
| C05 | `_enforce_keyword_density` uses skills found anywhere and samples the first five work bullets for insertion. | Require a fact linking the skill to the specific employer/project. Repair only lost supported wording. |
| C06 | `ContentAugmenter.augment` expands bullets, injects impact, and may generate projects according to page fill. | Select relevant source-backed content first. Project ideas stay outside the resume until completed/confirmed. Audit every generation mode. |
| C07 | `_finalize_with_quality` reviews an intermediate draft; its judge lacks source facts while evaluating metric preservation; repair is not quality-compared again. | Review final content with sources and requirements. Revalidate and compare repairs. |
| C08 | Augmentation follows primary integrity enforcement; final verification is advisory. `claim_guard` can change text in strict mode. | Separate read-only checks from explicit repairs; every semantic change invalidates affected checks. |
| C09 | New summary refresh tests `_backing_failures` and can retain existing text; summary still precedes downstream advisory verification. | Integrate the improved writer into whole-document revision checks, covering claim relationships and semantics, not only term backing. |
| C10 | Shared voice rule and summary prompt forbid a JD title as subject while requiring the resolved role label, which can equal it. | Permit supported normalized role labels. Ban unsupported seniority/specialization, not textual equality. |
| C11 | Fixed bullet character bands, exactly one technology per project bullet, verb quotas, and metric score weight remain. | Profession-neutral accomplishment review; measured layout. Technology/metric counts are not universal minimums. |
| C12 | "Preserve every source number" conflicts with selective omission. `fix_ai_tell_symbols` removes `~` and K/M `+`, despite preserving factual meaning. | Preserve metrics on retained claims, including approximation/comparators. `~5,000` becomes "about 5,000", never exact 5,000. |
| C13 | `claim_guard` uses resume-wide numbers and product term presence as grounding signals. | Add claim-level source links and employer/project scope; a number elsewhere is insufficient proof. |
| C14 | JD extraction takes a 5,000-character prefix; `_smart_truncate` can slice serialized JSON after dropping bullets. | Section-aware bounded extraction/retrieval; explicit incomplete state. Never silently treat truncated input as complete. |
| C15 | `ResumeScorer` gives lexical/skill measures 50% of its composite, rewards repetitions, and simulates named vendors and screening/culture fit. | Separate evidence coverage, human review, and observed artifact checks. Remove simulated vendor certainty and optimistic missing-data defaults. |
| C16 | Format defaults to 95. `score` attaches `render_error`, but PDF score capping runs only with bytes, contrary to the worker comment. | Missing bytes means failed/pending rendering, not good formatting. Test render and extraction failure separately. |
| C17 | `prepare_tailored_artifact` and jobs shortcut call tailor directly; other worker paths share or reproduce finalization. | One orchestrator for all adapters: initial, combined extraction, batch, guest, regenerate, jobs shortcut, auto-apply preparation. |
| C18 | Regeneration rewrites full JSON, restores skills, and augmentation may refill requested shortening; enrichment is not passed identically. | Freeze parent source snapshot; scoped patches; preserve user locks and omissions; identical evidence rules. |
| C19 | `canonical_content_hash` hashes the full resume dict, which can contain diagnostic sidecars. | Separate visible content identity from assessment/input/artifact hashes. Preserve historical hashes. |
| C20 | `recruiter_gaze_service` uses a technical recruiter persona and fixed attention assumptions. | Adapt review to profession/document structure; reuse advisory infrastructure without claiming universal human behavior. |
| C21 | Existing tests require old scanner keys/weights, coverage behavior, and formatting constraints. | Version contracts and update obsolete assertions alongside code; retain true integrity and export regressions. |

## Boundaries and verification

Naturalness audit: the primary tailoring prompt explicitly asks to rewrite every retained bullet, place 8–12 critical JD terms in work/project content, aim for at least ten metric-bearing bullets, and make every bullet STAR-defensible. `prompt_rules` adds mechanical verb diversity; `ContentAugmenter._fix_weak_verbs` performs another wording pass. These can overrule the same prompt's human-sounding/anti-stuffing instructions. Replace them with the architecture's naturalness contract, keeping useful original prose and repairing only diagnosed issues. Existing per-user `edit_signals` is a reusable style source, not a factual source.

Additional verifier conflict: `tailor_verifier._VERIFIER_SYSTEM` allows reworded facts, but PARTIAL combines plausible new details with generalizations not explicitly stated, while a separate rule broadly accepts role-standard activities. Split semantic support from new-detail uncertainty. A capability entailed by described work should pass without verbatim wording or user confirmation; merely typical duties should not. Do not revert valid reframing because one clause needs repair. See the architecture's validation refinement and adaptation review.

Keep ownership checks, entitlement checks before spend, async transport, provider abstraction, existing version limits, and source-fact protection. `fit_score` remains the sole candidate-to-job match formula; document assessments must not overwrite it or change submission authorization.

The audit inspected relevant orchestration, prompts, guards, scoring, versions, and selected tests. Production flags, full route census, live prose quality, and failure rates remain implementation-validation work. Tests in this document are requirements, not reported passes.
