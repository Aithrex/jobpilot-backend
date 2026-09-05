# JD-adaptive professional summary

Integration target: [unified tailoring architecture](../specs/evidence-led-tailoring-architecture.md). Preserve this workstream's implemented improvements while adopting shared source snapshots, whole-document validation, and versioned artifact checks.

Status: implemented. Runtime writer, sidecar, refresh, role matrix, and preview notice are in place.

This is a workstream within the [full ATS and human-review tailoring plan](2026-09-05-evidence-led-resume-tailoring.md). Follow that plan for shared evidence contracts, final pipeline ordering, scoring, and rollout.

## Intended behavior

The summary should position each user for the actual job, using only that user's supported experience. Choose the opening role label, capabilities, and supporting achievements from the user's resume and the target JD. Historical job titles, experience, and achievements remain factual.

This is a product-wide change for general users. The supplied resume illustrates the problem; it must not become a template or a source of special rules for everyone. No healthcare- or GenAI-specific controls or behavior are required. Findings below describe the existing implementation, not preferred defaults for users.

## Current architecture

1. `ResumeService.extract_jd` extracts the title, required/preferred skills, responsibilities, qualifications, years, industry, and keywords.
2. The tailoring flow resolves the base resume and calls `ResumeTailor.tailor`. The primary model writes bullets and skills with an empty summary.
3. `ResumeTailor._finalize` allocates bullets and invokes `_write_settled_summary`. Regeneration also invokes the deferred writer.
4. `summary_tailor.build_summary_brief` resolves a role label using title classification and bullet-family support, selects technologies, derives years and domain, and takes the first six experience bullets as generation evidence.
5. The summary model receives this brief, not the full structured JD. It generates once and retries after validation failure. A valid candidate is accepted; failure or exception restores the cosmetic-cleaned base summary.
6. Service finalization subsequently augments content, normalizes dates/titles, attaches advisory verification, and audits keywords. The preview reads `result.tailored_resume`.

The summary is deferred relative to primary tailoring, but precedes service-level augmentation. Its evidence is therefore not necessarily the final content delivered to the reader.

## Findings

- **The supplied wording is a documented reference, not a runtime string constant.** Its opener appears in `docs/specs/summary_content_model.md`. The runtime instead imposes a similar content structure.
- **General SDE targeting already exists.** `swe` maps to "Software engineer", and `_bullet_supports_family` counts all recognized engineering families as support for it. However, adopting this broader label still depends on the 50% support threshold, which is inappropriate when sparse taxonomy matches are the only obstacle to a truthful general engineering identity.
- **Base vocabulary outranks JD relevance.** `_must_retain` preserves up to eight base-summary technologies, and `allocate_tech_slots` selects them before JD keywords. Validation rejects drafts that omit those terms, including when they are unimportant for this job.
- **Domain and differentiator selection are insufficiently conditional.** `_domain_qualifier` returns the first supported resume domain without first evaluating target relevance. S3 prefers AI/ML terms or overflow languages; the prompt requires a third sentence whenever that slot is populated. It cannot freely choose reliability, product delivery, testing, or another relevant capability instead.
- **The writer loses useful JD context.** Responsibilities and qualifications are extracted upstream but are absent from the generation brief. A short keyword list does not adequately distinguish jobs with the same title.
- **Evidence selection is inconsistent.** Generation sees the first six experience bullets; allocation and validation inspect all experience and project bullets. The model can receive a required technology without the supporting bullet needed to write a credible claim.
- **Validation encourages fallback.** Mandatory retained terms, a technology-count floor, sentence rules, and a separate 500-character cap can reject otherwise useful prose. Generation errors, validation failure, and grounding-audit errors all restore the base, with reasons in logs but no summary-specific result status.
- **The documented acceptance rule is stale.** Current `write_summary` accepts `verdict.ok` regardless of relative score. Do not reimplement this already-present change. The documentation and some conditional tests still describe comparative acceptance.
- **Grounding policy has drifted.** The validator exempts JD keyword-budget terms from its bullet-backing check, despite the prompt requiring every technology to be backed by a bullet. The separate claim audit still runs, but the contracts should agree.

These are local-code findings. The exact cause of a particular unchanged production result requires its generation logs, selected base, extracted JD title, and deployed revision; it is not established by this review.

## Proposed implementation

### 1. Make target positioning explicit

Extend `SummaryBrief` with target title, role responsibilities, prioritized capabilities, selected evidence references, and the rationale for its display label. Reuse existing structured JD fields before adding another model call or changing the extraction schema.

Separate broad role positioning from specialist identity. For general SWE/SDE jobs, permit "Software engineer" when the candidate has supported engineering experience. For specialist titles, retain evidence requirements: backend experience alone must not create frontend, full-stack, ML research, or management credentials. Handle missing or ambiguous titles using responsibilities and a supported neutral label. Never copy unsupported seniority from the JD.

### 2. Rank evidence and content for the job

Replace mandatory retention of base-summary technologies with a JD-ranked selection of supported facts. Select evidence across experience and projects for relevant responsibilities, required skills, and demonstrated outcomes. Include the evidence behind every selected claim in the model context.

Select industry context and differentiating strengths through the same evidence-and-relevance ranking used for other content. Do not hardcode any user's industry, specialty, stack, or career narrative into the summary structure. Keep all specialist claims proportional to demonstrated scope.

### 3. Align generation and validation

Keep a concise two- or three-sentence summary, but let the brief choose the content and optional third sentence. Replace the universal eight-technology minimum with a small relevance-driven budget. Preserve strict checks for invented skills, employers, metrics, experience, and seniority. Use supported capabilities as evidence even when a sentence contains no named framework.

Align word and character constraints with the PDF layout, and use the same constraints in the prompt, validator, and documentation. Remove the JD-keyword exemption from evidence checks. Accept a valid tailored draft without restoring the old base-score competition.

### 4. Make fallback visible and evidence final

Return a summary diagnostic alongside existing result metadata: selected role label, generated versus base fallback, attempts, and reason codes. Avoid storing raw prompts or sensitive resume text in diagnostics. Surface a short notice in the preview when generation falls back, without blocking the rest of the resume.

Preserve a safe base fallback after a bounded retry. Never force a change merely to make the text different. Revalidate summary backing after augmentation; if content changes invalidate its evidence, refresh it once from the final content before final verification. Cover direct tailoring callers as well as service orchestration to avoid missing or duplicate generation.

### 5. Verify behavior across jobs

Add a multi-user test matrix with varied experience levels, industries, and role families, including technical and nontechnical profiles. For each profile, compare summaries across relevant JDs. Include broader versus specialist roles, different responsibilities under the same title, absent titles, sparse evidence, and supporting project bullets outside the first six experience bullets. The supplied backend-to-SDE example is one regression case, not the entire evaluation set.

Assert role positioning, relevant fact selection, preservation of factual boundaries, and absence of assumptions imported from another user's profile. Verify that nontechnical profiles can produce valid summaries without a technology quota. Test retry and fallback diagnostics, acceptance of a valid draft with a lower base-relative score, and evidence consistency after augmentation. Add a controlled generation evaluation for prose quality; deterministic mocked generation alone cannot establish that summaries meaningfully adapt.

## Delivery order

1. Confirm the affected runtime revision and capture fallback diagnostics.
2. Implement the brief, evidence ranking, and broad-versus-specialist role policy.
3. Update generation and validation together, including final evidence checks.
4. Expose fallback status, update architecture/content docs, and run the role matrix plus integration and render checks.

Expected outcome: each user's summary reflects the target role and its responsibilities while remaining grounded in that user's own career history. It should adapt meaningfully across JDs without inventing qualifications or imposing a shared career narrative.
