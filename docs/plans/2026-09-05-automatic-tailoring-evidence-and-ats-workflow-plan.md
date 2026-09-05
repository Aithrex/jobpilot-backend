# Automatic tailoring and resume assessment: implementation plan

Date: 2026-09-05. Status: proposed implementation following source inspection; no runtime changes or production enablement in this task.

This extends the ATS engine master plan and supersedes manual evidence attachment as the normal tailoring workflow. Reuse the existing planner, requirement contracts, renderer, reader, and assessment storage. Do not build another scoring engine.

## Product objective

Produce the strongest clear, truthful presentation of the candidate's relevant experience with minimal user effort. Automatically connect existing source experience to the final resume. Users should not have to identify evidence that the system already has, append keyword clauses, or repeatedly rescore to finish an application.

JD alignment can improve searchability and relevance, but cannot guarantee a universal ATS score, filter pass, or interview. Greenhouse documents both exact keyword filtering and a separate employer-calibrated matching feature. Its application-question rejection rules are another mechanism. These are distinct from our document checks:

- [Talent filtering](https://support.greenhouse.io/hc/en-us/articles/27104809835291-Talent-Filtering)
- [Talent matching](https://support.greenhouse.io/hc/en-us/articles/41396009937307-Talent-Matching)
- [Application-question auto-reject](https://support.greenhouse.io/hc/en-us/articles/360000653472-Auto-reject)

Optimize supported requirement presentation, recognizable terminology, actual file readability, and human clarity together. Do not promise the "best ATS score" or optimize a fabricated vendor score.

## 1. Findings in the current tree

These are static source findings, not production telemetry. The acceptance report's 181 passing tests were read, not rerun during this audit. The screenshot matches the legacy UI branch; it does not establish deployed flag values.

| Finding | Code | Consequence |
| --- | --- | --- |
| Lexical missing requirements are passed to a model forced to choose defensible, learnable, or stretch. There is no already-demonstrated correction category. | `shared/python/services/match_gap_reporter.py`, `_build_prompt`, `_coerce_tiers`, `build_match_gaps` | A rationale can recognize explicit experience while its card still calls it missing. This explains a mechanism consistent with the screenshot; the exact request payload was not replayed. |
| Classification failure becomes learnable; gap detection failure returns an empty report with default 100 coverage. | Same file, fallback helpers | Processing uncertainty becomes a candidate gap or apparent perfect coverage. |
| UI demands a clause and a target bullet; its new-bullet option defaults to the first available entity. | Frontend `src/app-shell/pages/craft/tailor/MatchGapsPanel.tsx` | Extra user work and potential attribution to the wrong employer/project. |
| Attaching evidence appends text, inserts a skill, then asks the user to rescore. | Frontend `TailorPage.tsx`, `handleAttachEvidence` | A local resume mutation substitutes for automatic evidence reconciliation. Clause length and selection are not semantic verification. |
| UI says Skills earns no credit, while v2 has requirement-specific evidence expectations including listed skills. | `MatchGapsPanel.tsx`; `assessment_policy.py` | The interface applies a blanket rule that conflicts with the assessment contract. |
| Three assessment branches coexist; evidence-tailoring results return from `ResumeScorer.score` before its v2 flag branch. V2 exceptions can fall back to hybrid scores. | `resume_scorer.py`; frontend `AtsScorePanel.tsx` | Result policy can depend on entry point and failure, rather than the selected canonical policy. |
| Finalize overlays v2 on evidence-tailoring results without record/version identity in that call, reaching the detached branch. That branch uses the deterministic reader. | `resume_service.py`, `_finalize_tailored_result`; `assessment_service.py`, `score_payload_v2` | A canonical Claude reader exists, but is not uniformly used. Audit later persistence/routing before claiming every flow has the same checks. |
| Missing document coverage directly sets source state to not_evidenced; source reconciliation runs for satisfied/partial document states. | `requirement_assessment.py`, `evaluate_requirements` | The engine cannot reliably distinguish source-supported experience omitted by tailoring from absent source experience. |
| Source facts default to `source_resume or resume`; canonical stages call evaluation without an independent source. | `requirement_assessment.py`; `assessment_service.py`, `run_assessment_stages` | Generated output may be used to support its own claims. An explicitly empty source also falls through. |
| Owned adapter re-evaluates with source after persisted stages and modifies the returned evaluations without rebuilding reader/comparison or persisting that revised result there. | `assessment_service.py`, `score_payload_v2` | Returned findings and stored assessment can describe different evaluations. |
| Detached content hash covers only summary; fallback document text omits some sections. Revision helper generates PDF regardless of requested format. | `assessment_service.py`, detached snapshot/text builder and `assess_resume_revision` | Bullet edits can escape that hash, projects can be missed, and format identity can be wrong. |
| Observation maps use capability alone even though predicates can have different qualifiers. | `requirement_assessment.py`, `leaf_obs` and `pred_meta` | Repeated capabilities with different ownership/duration expectations can overwrite each other. Add a regression test and key by full predicate identity. |
| Tailoring has a bounded repair for quoted over-tailoring, but no equivalent canonical feedback loop for supported experience missing from the final document. | `tailoring_pipeline.py`, final repair block | The system can simplify prose but still leaves presentation gaps for the user to resolve. |

## 2. Target workflow

1. Parse the candidate's source resume and any previously confirmed experience once. Preserve employer/project, dates, scope, and source locations.
2. Parse the JD once into canonical requirements, including AND/OR alternatives and qualifiers. Separate required and preferred requirements; do not count repetitions as additional requirements.
3. Independently map source support to each requirement. Keep the mapping internal, reusable, and scoped to the actual job/project. Missing evidence is not automatically lack of ability.
4. Plan the resume around the most relevant supported accomplishments. Adapt summary positioning to the role; preserve official employment titles and factual scope. Industry and GenAI emphasis follow the candidate and JD, with no fixed profession assumptions.
5. Write natural contextual bullets and a concise skills section using supported terminology. Use clear JD names where accurate. Never turn adjacent experience into claimed use of a different tool.
6. Assess the draft's actual requirement presentation and source support separately. Detect meaningful omissions, unsupported changes, lost accomplishments, repetition, and awkward keyword additions.
7. Make at most one bounded automatic repair of unlocked content for supported omissions, accurate terminology, or clarity. Validate changed claims, preserve locks/omissions, and reject regressions. A standalone Recheck never rewrites content.
8. Render the requested PDF/DOCX, inspect actual extracted text and associations, and complete the reader check on that final revision. If rendering changes content, invalidate affected checks. Associate results with exactly the delivered file.
9. Show the resume and a compact assessment. Optional missing-experience input stays secondary. Save/download/apply should not require manual evidence attachment or reaching an arbitrary score.

Source records establish what the writer may claim. The final document establishes what the employer can see. Hidden source evidence must not receive final-document coverage credit.

## 3. Shared requirement states and automatic actions

Derive these action states from canonical source/document evaluations; do not create another LLM gap classifier.

| Source and final document | System action | User experience |
| --- | --- | --- |
| Supported and clearly shown | Link source and final spans automatically | Covered; details expandable, no task |
| Supported but omitted/unclear | Attempt a scoped automatic repair before completion | Included automatically, or optional suggestion if locked/space-constrained |
| Demonstrated semantically but terminology could be clearer | Add/replace with an accurate recognizable term only when natural | No false missing-skill card; terminology improvement is not new experience coverage |
| Related experience only | Describe the actual transferable capability | Do not claim a named tool, credential, ownership, or duration not supported |
| Not found in complete source | Leave unsupported claim out | Optional “Have relevant experience to add?”; may skip |
| Source/extraction/check incomplete | Retry within job budget or return unknown/partial | Explain incomplete check; no invented gap or default high score |
| User lock or omission prevents a change | Preserve it | Optional localized suggestion, no repeated prompts |

For skills-list expectations, an accurate source-supported Skills entry may satisfy the requirement. For production-use, ownership, or duration expectations, require the appropriate contextual evidence. Never demand a work bullet for every skill.

## 4. Implementation packages, in dependency order

### A. Fix source and assessment correctness first

Files: `requirement_assessment.py`, `assessment_service.py`, `assessment_contracts.py`, `assessment_policy.py`.

- Evaluate source and document independently for every requirement, including document absence. Use explicit source availability, not truthy fallbacks to generated output.
- Resolve original source and confirmed additions from the owned record or immutable draft snapshot before all stages. Preserve entity linkage and source revision.
- Identify predicates by capability plus qualifiers/context, not just capability text.
- Run source checks, reader, comparison, and persistence from one final result. Remove the post-persistence replacement of evaluations.
- Hash the complete normalized resume plus source, JD, rubric, renderer/template, format, and artifact dependencies. Include every visible section in the shared text builder.
- Dispatch PDF/DOCX rendering correctly. Keep structured-draft checks distinct from verified file checks.

Exit: source-supported omitted experience is recognized without user action; absent source never self-validates generated claims; returned, stored, and reopened results agree.

### B. Unify routing and policy

Files: `resume_scorer.py`, `resume_service.py`, `tailoring_assessment.py`, canonical service, relevant routes and job adapters.

- Pin one assessment policy per job/revision. Route evidence-tailoring, normal tailor, regenerate, guest, batch, job search, edit/recheck, save, download, tracker, and prepare/apply through the same stage implementation.
- Detached and persisted drafts use the same evaluation logic and reader policy; ownership/storage are adapters, not different assessment engines.
- Under v2, failures remain v2 partial/unavailable. Never silently switch to legacy numeric results.
- Avoid duplicate reader/render calls from evidence-v1 finalize followed by the v2 overlay.
- Maintain a caller/flag matrix including both current flags. New writers must not unexpectedly be measured by a different policy. Historical results retain their version and can be explicitly rechecked.
- Preserve asynchronous progress/cancel behavior, quotas, ownership, idempotency, and version checks.

Exit: integration tests prove each caller returns the pinned policy and the same revision semantics, including failure paths.

### C. Add automatic presentation reconciliation

Files: `tailoring_planner.py`, `tailoring_pipeline.py`, `tailoring_jd.py`, shared action builder derived from canonical evaluations.

- Share stable requirement and fact identifiers between planner and assessor.
- Automatically find the correct existing source bullet/project for supported requirements. Reuse valid semantic mappings instead of asking the user to locate them again.
- Build a small repair set for important supported-but-hidden requirements, inaccurate naming, and quoted readability issues. Prefer a substitution or clearer phrase over appending clauses.
- One repair pass with an explicit time/token budget, followed by affected checks and final artifact validation. Stop when no defensible improvement remains; never loop toward a target score.
- Preserve concrete actions, outcomes, scope, and voice. Keep metrics only when sourced. Preserve locks, exclusions, shortening, and the intended resume length.
- Treat safe terminology improvements separately from new coverage. No repeated-term reward, requirement stuffing, or keyword quota per bullet.

Exit: the REST API/OpenAPI screenshot cases finish automatically with no evidence form when the source supports them. A genuinely unsupported technology remains absent.

### D. Replace the manual evidence UX

Files in frontend: `MatchGapsPanel.tsx`, `AtsScorePanel.tsx`, `AssessmentV2Panel.tsx`, `TailorPage.tsx`, API/types and corresponding batch/saved-result surfaces.

- Retire “You likely have these,” blanket “Skills earns no credit,” and mandatory attach-clause controls for new results.
- Display compact statuses for file readability, requirements shown, and readability concerns. Evidence locations are optional expanded details, not homework.
- Collapse optional missing-experience discovery. Ask only about high-value unknowns, persist confirmed facts for reuse, and require the actual job/project when adding new experience. Never silently choose the latest role.
- Remove the attachment handler's automatic skill insertion as the default gap-resolution mechanism.
- Recheck edits automatically with debounce/cancellation and revision matching; retain an explicit retry control. Show stale/checking state until the matching revision finishes. Do not rewrite user edits during recheck.
- Display important concerns without making every advisory finding block download/application. Keep existing genuine safety and authorization restrictions.

Exit: ordinary tailor-to-download requires zero evidence-attachment interactions. Existing evidence never prompts for confirmation again.

### E. Measure usefulness without score inflation

- File: actual extraction completeness, reading order, employer/title/date/bullet association, and format-specific limitations.
- Requirements: required/preferred satisfied, partial, not shown, unknown, and unmet qualifiers. Show counts with a defined denominator, not a claim of employer pass probability.
- Credibility: source support and scope consistency. Do not equate unsupported with false; preserve unknown where appropriate.
- Human review: clarity, specificity, retained accomplishments, repetition, unnecessary polish, and scanability.
- Search terminology: source-supported important names present naturally; separate this diagnostic from semantic capability coverage.
- Keep candidate/job fit in the existing fit service. Do not mix readiness, missing qualifications, and file checks into a new simulated vendor score.

Exit: a cosmetic keyword insertion cannot raise capability coverage; repeating keywords never improves the assessment; missing checks never become success.

### F. Regression and release acceptance

Extend existing assessment, caller-routing, and workflow suites. Add meaningful fixtures for:

1. Explicit REST endpoint development versus JD “REST API Development”: automatic match and correct entity, no manual prompt.
2. Explicit OpenAPI versus compound JD labels: parse exact alternatives correctly; do not infer use of Swagger tooling from any API work.
3. Real source experience omitted during tailoring: source remains supported, final coverage absent until repaired.
4. Generated claim with missing/empty original source: unverified, never self-supported.
5. Same capability with different ownership/duration qualifiers: independent observations, no overwrites.
6. Skills-only requirement versus production-use requirement: distinct evidence expectations.
7. React versus Angular, AWS versus Azure: transferable context does not become claimed product use.
8. Locked or excluded experience: preserved through repair, recheck, save, and regeneration.
9. Bullet-only edit: assessment hash changes; stale results cannot attach to the new revision.
10. Projects, education, credentials, PDF and DOCX: complete text and correct associations, actual exported format.
11. Reader outage and v2 exception: same policy with partial/unavailable, no hybrid fallback.
12. Keyword repetition and generic polished prose: no coverage gain; retain substantive achievements.
13. Tailor → automatic repair → edit → recheck → save → download → reopen: one consistent source/document/artifact lineage across all callers.

After offline checks, run the master plan's live multi-profession pilot, blinded human review, and visual export acceptance. Measure false-gap rate, unsupported additions, source-to-document attribution errors, manual interaction count, edit preservation, latency, cost, and reviewer preference against the current workflow. Set acceptance thresholds before holdout review. Do not use average internal score improvement as the release gate.

Keep `RESUME_ASSESSMENT_V2_ENABLED=false` for broad production until the correctness/routing regressions and existing live release gates pass. The existing four-sample offline shadow result is not a substitute. Roll out to a controlled cohort with a tested rollback after acceptance.

## Completion definition

The feature is complete when it automatically surfaces relevant existing experience, delivers a natural and searchable final document, accurately identifies remaining uncertainty, and preserves the same checked revision through the application workflow. A high score alone is not completion, and a mismatch between a candidate and a JD should remain visible rather than being edited away.
