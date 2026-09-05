# Tailoring implementation and validation

Implemented locally on 2026-09-05. Not deployed. The new flow is opt-in through `EVIDENCE_TAILORING_ENABLED=true` on the backend and its worker. Without that setting, initial tailoring retains the legacy flow. Deploy the compatible frontend before enabling the backend flag. Historical score records remain readable.

## Implemented behavior

- `tailoring_policy.py` owns the new writing and reader policies. The new path has no keyword coverage percentage, repetition target, forced metric count, technology floor, or requirement to replace every action verb.
- `tailoring_jd.py` extracts source-linked required/preferred/unspecified requirements, alternatives and job context in bounded whole chunks. Truncated or failed analysis is marked partial; new HTTP flows reject inputs above the supported 56,000-character budget instead of silently treating a prefix as complete.
- `tailoring_planner.py` represents accomplishments under their actual job or project. Listed skills, demonstrated capabilities, related experience and gaps remain distinct. `tailoring_pipeline.py` adds semantic matching for transferable actions across professions. Users do not answer evidence questions for normal rephrasing.
- The writer can substantially reframe supported actions, select relevant bullets and reorder existing skills. It cannot create employers, roles, dates, technologies, metrics or ownership. Per-bullet validation receives the relevant source bullet only. Changed quantities and approximation qualifiers are rejected deterministically; semantic changes receive an independent entailment check. Missing verification retains source wording.
- The existing JD-adaptive summary writer is retained. The new policy removes its engineering technology floor and generic voice prescription. Healthcare, GenAI and other domains are not mandatory identities; existing JD-ranked summary positioning decides relevance.
- Every `ResumeTailor` entry point dispatches to the same new runner under the flag. Legacy augmentation, date/title normalization, summary refresh and keyword audits skip its reviewed results. The job-search shortcut and standalone bullet rewrite dispatch background jobs under the flag; the shortcut now uses the shared quota reservation and worker.
- One focused repair can address reader-quoted over-tailoring in generated bullets. It reruns scoped factual validation, summary generation and final review. A repair failure preserves the preceding reviewed draft; no unchecked global polish runs afterward.
- `tailoring_assessment.py` separates source requirement coverage, reader feedback and actual extracted export text. Reader findings require an exact visible quote and do not see the source fact map. No estimated Workday/Greenhouse scores or screening probability are emitted for new results; `overall` is null.
- PDF and DOCX checks compare extracted text against contact details, identity fields, bullets, skills, summary and certifications. Export metadata includes content and byte hashes. New downloads persist their format-specific check under layout metadata. An unsaved download cannot overwrite a saved version's object key.
- Content changes and JD changes invalidate assessments. Manual wording locks, summary shortening and explicit bullet omissions survive regeneration. Added manual prose is retained but remains unverified. Save and download persistence check for concurrent record updates. Metadata-only reviews do not create duplicate content versions.
- Standard worker persistence freezes the selected structured source in the server-owned record. Refinement can use that snapshot even if the active base resume changes. It is excluded from record API serialization.
- Optional discovery questions focus on skills whose actual work context is missing. Users can confirm additional real work under a specific existing job/project. The confirmation endpoint verifies ownership, bounds input, and stores the user's exact text against the frozen source hash. It enters the next refinement as additional scoped evidence; the UI does not wait for answers during generation.
- The new review panel replaces score charts for new results, shows stale/unknown checks explicitly, and provides the optional experience form. Refinement suggestions now ask for relevance, clearer contributions and natural language instead of keyword or metric insertion.

## Validation

Offline backend tests cover meaningful semantic reframing, alternative requirements, profession-neutral capability matching, metric qualifiers, unsupported changes, unavailable validation, role-scoped additions, owner access, locked edits, repeated refinement, omissions, review invalidation, reader quote validation, bounded repair, version hashes, both tailoring entry points and actual PDF/DOCX extraction. Regression suites also cover existing summary behavior, prompt contracts, profile facts, resume platform/versioning and auto-apply preparation.

The combined primary backend run passed 168 tests. An additional overlapping run covering augmentation, verification ordering and document layout passed 84 tests. After adding rollback/context-isolation cases, all 24 new backend tests passed; the final auto-apply preparation plus new-policy run passed 60 tests. The focused frontend run passed 17 tests across three files. The PDF and DOCX checks use real generated bytes in addition to the mocked provider/control-flow cases.

Frontend tests cover review separation, unknown and stale states, source-id preservation after removal, manual wording locks, summary shortening and existing summary fallback display. The production Vite build passes. Repository-wide `tsc -b` still reports errors in unrelated integration, job-search, interview-prep and existing API types; this is not a clean repository-wide type-check result.

No live model benchmark, external ATS trial, human recruiter study, production deployment, or visual browser acceptance session was performed. Offline model mocks establish control-flow and integrity behavior, not subjective writing quality or hiring outcomes.

## Practical rollout limits

1. Keep the flag off in production until representative live before/after resumes have been reviewed across software, operations, finance, sales, career-change and early-career cases. Measure latency, cost, unsupported-claim rate, meaningful adaptation and reader preference; do not use keyword counts as the success criterion.
2. Semantic matching and entailment are model judgments. They can miss a connection or disagree. Missing validation retains source text; ambiguous/new detail is not silently promoted into a fact.
3. The optional form currently covers existing jobs/projects on records with a frozen source. It does not automatically attach imported LinkedIn/GitHub or voice-graph material to a role without an explicit role relationship. Those imports need a separate ownership/context mapping before broader use.
4. Export checks establish extracted text presence, not full visual quality, reading order, or compatibility with every employer's parser. The actual downloaded DOCX has its own checks; a PDF review is not a DOCX certification.
5. Source snapshots are established by the standard worker persistence. Old records lacking snapshots continue to resolve their pinned base. Live rollout should test guest claiming, batch persistence and existing-record migrations with real storage; no bulk database migration is part of this change.
6. Manual additions remain unverified until incorporated as candidate-confirmed source experience. The new pipeline preserves them instead of silently reverting or treating an edit as independent evidence. Automated preparation does not mark an artifact ready when checks are stale, reader review is unavailable, or material reader concerns remain.
7. The repair is deliberately bounded to one pass and six affected generated bullets. Remaining reader concerns stay visible. There is no claim that every draft is perfect or will pass ATS/human screening.

## Rollout and rollback

Deploy the frontend first, then enable `EVIDENCE_TAILORING_ENABLED=true` for a controlled backend/worker environment. Exercise first-time tailoring, refinement with locks, optional discovery, scoring, downloads and saved-version reopening together. Roll back the flag for new runs if integrity, latency or user-readability thresholds regress. Preserve existing records and assessments; do not rewrite historical vendor-style scores into new assessments.
