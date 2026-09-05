# ATS measurement audit and implementation plan

Execution checklist: [ATS engine master implementation plan](2026-09-05-ats-engine-master-implementation-plan.md). Use that consolidated plan for implementation; this document retains the audit findings and measurement rationale.

Status: audited current working-tree code on 2026-09-05. This document proposes the next measurement changes; it does not implement them. Scope is the tailored-resume assessment, its exports, API, persistence and UI. Application-submission adapters and the candidate/job matching formula are not being redesigned.

Revised after [adversarial reverse review](2026-09-05-ats-plan-reverse-review.md). The corrections below supersede the earlier public visibility-percentage proposal. The review found measurement and implementation gaps; it is not an empirical ATS or recruiter validation.

## Decision

Measure how well the final, readable document presents the candidate's supported fit for this job. Keep four distinct results: document parsing, requirement presentation, claim support, and reader quality. Show application eligibility separately when it is known.

Do not create another opaque 0–100 ATS score. Keep `fit_score.py` as the only candidate-to-job match formula. A resume can communicate its limited relevant experience perfectly while still being a weak match. Conversely, a strong candidate can have a poorly presented resume. The interface must expose that distinction.

The strongest tailoring improvement is making previously hidden, relevant experience clear. More tokens, more metrics and closer copying of the JD are not improvements by themselves.

## What the current code actually does

| Area and code | Finding | Conflict / change required |
|---|---|---|
| `resume_scorer.py:79`, `tailoring_pipeline.is_result` | A result carrying the new policy marker takes the new assessment branch. Unmarked results still take the hybrid legacy branch. | The migration is incomplete. Reassessment must use a canonical server-resolved policy rather than allowing marker presence to select the measurement contract. |
| `resume_scorer.py:616` | Overall weights are keyword match 25%, frequency 5%, skills alignment 20%, AI relevance 15%, AI screening 10%, metrics 10%, bullet quality 5%, format 5%, sections 5%. | Half the score is lexical and partly double-counts the same signal. This rewards the behavior the new writer is trying to avoid. |
| `resume_scorer.py:242` | Repeated keywords receive more credit; all aliases contribute to occurrence counts. | An acronym plus its expansion can count as repetition without demonstrating any additional capability. Remove frequency from quality scoring. |
| `resume_scorer.py:264` | A skill in Skills, a summary, or a work bullet can receive the same alignment credit. | Mention is treated as demonstration. Requirements must specify the level of evidence needed. |
| `resume_scorer.py:150` | Parked domain terms are restored to the required-skill list. | Inferred context can become a mandatory requirement. Preserve explicit priority and source wording. |
| `resume_scorer.py:186`, `:317`, `:358` | Format starts at 95; summary/skills/experience/education each earn 25 section points; metric occurrences and diversity earn impact points. | Unknown format looks verified; intentionally removing a summary is penalized; qualitative and early-career resumes are structurally disadvantaged. |
| `resume_scorer.py:444` | AI is instructed to generate six vendor scores, screening likelihood and culture-fit scores, with suggested numeric bands. | These are not employer measurements. Remove from newly computed assessments, including rescoring older documents. |
| `resume_scorer.py:633` | Missing AI results contribute defaults of 70 to parts of the overall calculation. Schema coercion can return zero-valued displayed AI/vendor fields. | Missing observations become contradictory numbers instead of unknown. |
| `tailoring_assessment.py:91` | Copies requirements and provenance from `tailoring_run`. Visibility checks use stored bullet text and resume JSON for skills/education/certifications. | Final-file coverage is not independently established. Something missing from the export can remain marked visible. Source mapping must be distinguished from document presentation. |
| `tailoring_assessment.py:102`, `tailoring_planner.semantic_support` | Final visibility has a few deterministic semantic rules even though the planner can recognize broader semantic relationships. | A valid nontechnical reframe recognized during planning can be reported as hidden during assessment. Reuse the requirement contract, not a narrower vocabulary test. |
| `tailoring_assessment.py:67` | Reader gets selected legacy JD fields, not the full canonical requirement groups. | Alternatives, logistics and explicit requirement nuance can be lost between planning and review. Give the reader the same employer requirement definitions, but never hidden candidate evidence or writer rationales. |
| `tailoring_assessment.py:18` | PDF text and DOCX paragraphs/tables are checked by whitespace-stripped substring presence. | Presence does not establish correct word boundaries, reading order, employer/date association or visible layout. DOCX extraction puts all paragraphs before table contents instead of preserving document order. |
| `tailoring_assessment.py:120` | `ready` requires fresh hashes, no marked manual edits, export passed, reader completed and no concern/over-tailoring findings. It does not include analysis/source completeness. | Incomplete analysis can look ready; a generic reader concern can become a universal block. Separate execution status, finding severity, analysis completeness and automated-use eligibility. |
| `tailoring_assessment.py:124` | Finalization reuse keys on document and JD hashes. Reader cache also uses byte hash. | Rule/model/renderer changes are not fully represented. Separate artifact cache from semantic-review cache and version both. |
| `resume_versioning.py:132`, save/score routes | Some content-hash invalidation exists, but assessment sidecars and scores travel through client-provided payloads. | Resolve assessment ownership and version server-side; validate assessment IDs, hashes and dependencies before attaching them. Client metadata must not be authoritative evidence. |
| `pdf_ats_check.py`, `ats_vendor_checks.py` | Existing helpers include useful date/glyph/section checks alongside claims about universal parser behavior, vendor quirks and preferred section order. | Reuse observed checks selectively. Do not turn undocumented vendor assumptions or optional sections into hard ATS rules. |
| Frontend `AtsScorePanel.tsx`, `DocumentAssessmentPanel.tsx`, `types/resume.ts` | New panel separates checks; legacy panel still shows scanner numbers. Types describe several generations and still require legacy numeric fields on new responses. | Introduce a discriminated assessment contract and label historical results as legacy estimates. Audit tracker cards, deltas, sorting, empty states and exports together. |

### Executed deterministic probes

Using the current `ResumeScorer` without model calls:

| Probe | Current result |
|---|---|
| Frequency for one `AWS` mention | 50 |
| Frequency for one `Amazon Web Services (AWS)` mention | 100 |
| Keyword coverage for an empty requirement list | 100 |
| Alignment for Kafka listed only in Skills | 100 |
| Format before examining a file | 95 |
| Combine with no AI result | Relevance defaults to 70; displayed AI/vendor fields coerce to zero |

These are implementation observations, not results from a real ATS. No production code was changed for these probes.

## External reality check

Greenhouse documents parsing failures involving document formatting and file characteristics. A local extraction test is useful, but is not evidence that every employer parses the same way. [Greenhouse parsing documentation](https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse).

Some ATS products do provide matching scores/categories. Greenhouse Talent Matching uses employer-defined criteria and weighting and explicitly leaves hiring decisions with people. Therefore our problem is not that ATS matching never exists; it is that we do not possess an employer's configured evaluation. [Greenhouse Talent Matching](https://support.greenhouse.io/hc/en-us/articles/41396009937307-Talent-Matching).

Greenhouse also documents rejection rules based on application-question answers. Resume wording cannot fix a genuine license or other eligibility conflict. Keep known application criteria separate from resume quality; missing information is unknown, not a confirmed rejection. [Greenhouse auto-reject documentation](https://support.greenhouse.io/hc/en-us/articles/360000653472-Auto-reject).

## Proposed measurement model

### 1. File readability: observed checks, not an estimated vendor score

For the exact PDF or DOCX being assessed, report `passed`, `issues`, `failed`, or `unchecked`, with critical versus advisory findings. Record file hash, renderer/extractor version and document version.

Check extraction, word boundaries, contact preservation, expected content retention, section recognition appropriate to the actual resume, reading sequence, and company/title/date/bullet grouping. Compare the parsed structure with the intended structure. Check glyphs and joined words without normalizing away the defect. Treat missing optional phone numbers, summaries, projects or education as not applicable when appropriate. An intentionally removed bullet is not export loss; a renderer-dropped bullet still present in the intended document is.

Preserve the existing renderer's clipping/dropped-content outcomes. Validate PDF and DOCX independently. Add representative visual regression checks and a second independent extractor for validation cohorts; disagreement must be visible. A successful extraction from one library is a local observation, not a promise about vendor acceptance.

### 2. Requirement presentation: separate the source from the page

Freeze the canonical JD requirements before scoring either the base or tailored version. Each requirement has an ID, exact source span, priority, evidence expectation, explicit AND/OR structure, and qualifiers attached to the appropriate predicate. Deduplicate equivalent requirements. `Python or Java` is one alternative group; `Python and SQL` has two required components within its group. Report group counts consistently and expand components for explanation. Do not inflate the denominator because an employer repeats a requirement. Ambiguous Boolean wording stays unresolved rather than silently choosing an employer interpretation.

For each requirement maintain independent fields:

| Field | Values / meaning |
|---|---|
| Source support | demonstrated, listed only, related, not evidenced, contradicted, unknown |
| Document presentation | demonstrated clearly, mentioned only, related experience visible, supported but hidden, absent, unassessable |
| Match method | exact, recognized alias, semantic; retain quoted evidence and rationale |
| Claim validation | supported explicit, supported semantic, unverified new detail, contradicted |
| Location | exact extracted-file span plus actual job/project/education/skills section |

Full presentation credit depends on the requirement's evidence expectation. A tool list can establish that the candidate lists a skill. It cannot establish three years of production use. A degree requirement is supported by the actual education entry, not a software-project bullet. A credible qualitative accomplishment can fully demonstrate a responsibility; a numeric outcome is not mandatory.

Evaluate action, actor, ownership, negation, stated context and explicit duration/currency, not just shared terms. Do not infer continuous tool use from tenure. Aggregate requirements may use multiple separately scoped source facts; overlapping date intervals cannot be double-counted. A migration away from a tool can demonstrate prior use, while an evaluation alone does not establish deployment. Demand only the qualifiers the employer actually states.

First identify what the final extracted document says, without the source plan's proposed matches. Then reconcile those document claims with the frozen source evidence. This keeps the writer's intended meaning from being mistaken for meaning actually visible to a reader.

Count each requirement once. An accurate paraphrase can earn the same presentation credit as an exact phrase. Repeating it in Skills, summary and three bullets earns no additional coverage. A source fact may support multiple genuinely distinct requirements if each relationship is defensible; duplication alone must not create more requirements or evidence.

### 3. Measure changes without manufacturing a score improvement

Primary interface: show requirement counts, with required and preferred items separate. For example, `Required: 6 clearly demonstrated, 1 mentioned only, 1 related, 2 not evidenced in the source`.

Launch with requirement states/counts and concrete before/after findings. Classify changes as coverage gained/lost, clarity/discoverability improved, or new source evidence added. If both original and tailored wording semantically demonstrate the same capability, coverage stays equal. Improved ordering or concision can still be useful; a good unchanged resume is a valid outcome.

Keep **Supported evidence made visible** as an experimental internal diagnostic, not a public launch metric:

`100 × clearly presented supported requirements / source-supported presentable requirements`

The denominator is established from the frozen source and canonical JD before generation. It includes requirements for which source support meets the requirement's evidence expectation, not everything loosely related. A listed skill qualifies only if the requirement merely asks for that skill; it does not qualify for explicit production-use or duration requirements.

Required and preferred figures stay separate. No uncalibrated 0.5/0.75 partial-credit weights: show partial/related counts alongside the strict figure. The number is null when its denominator is zero or when source/JD analysis is incomplete. If extraction fails, document visibility is unassessable, not zero candidate ability. Display numerator and denominator, not just a percentage.

An internal 4/7 to 6/7 comparison is valid only if the same evaluator genuinely identifies two previously unexpressed requirements under a common source/JD basis. Adding a capability label to an already demonstrative action is not evidence of that gain. Public percentages require calibration and user-understanding validation first. Partial analysis still exposes completed item findings and unresolved items, but suppresses aggregates.

An unrelated candidate with one supported requirement could score 1/1 on visibility. Consequently this metric must never appear alone as “100% ATS match,” drive job ranking, or replace `fit_score`. Show all-JD coverage counts beside it. Do not hide genuine requirements to increase the number; exclusions remain explicit and the denominator remains fixed for a comparison.

Coverage is also not the writer's optimization target. Preserve concrete accomplishments and reasonable page/attention constraints. More coverage at the cost of a bloated summary or weaker contribution detail can be a regression. Intentional lower-priority omissions are visible tradeoffs, not instructions to refill the document.

### 4. Search-term discoverability: advisory and limited

Recognize exact terms and established aliases in the exported text. Where a supported named skill is unnecessarily hard to find, suggest one natural mention in its correct context. An accurate `PostgreSQL`/`Postgres` alias should not be a gap. A related tool is not an alias.

Separate “the capability is demonstrated semantically” from “the employer's named term is present.” The latter is an advisory search diagnostic, not a bonus for repetition. Never require adding named tools unsupported by the source. Do not rewrite a clear capability sentence merely to reproduce a long JD phrase.

### 5. Claim support: a trust result, not a writing restriction

Assess added factual claims against the actual source job/project and confirmed additions. Ordinary rephrasing and action-based capability interpretation need no user interruption. Preserve metric qualifiers, contribution, tools, causality, dates and scope.

Return counts and actionable exceptions. `Unverified` means a check could not establish support; it is not an accusation of fabrication. “Supported by your source” is not independent verification of employment or credentials. User edits remain intact. A recheck should validate affected claims and context dependencies without invoking the writer or asking for evidence again when the existing source already supports them. Changing a heading can invalidate associations for unchanged bullets beneath it. Offer optional clarification only for material new facts, alongside the usable draft.

### 6. Reader quality: independent, quote-backed review

The reader sees the final extracted resume and canonical JD requirements, not hidden facts, the writer's plan, scores or intended matches. Review role positioning, concrete contribution, relevant accomplishments, clarity, natural wording, needless repetition and implausibly uniform tailoring across prior roles.

Separate `review_status=completed` from findings such as `clear`, `minor_edit`, `material_concern`, `unassessable`. Model completion does not mean a positive judgment. Wording findings cite exact resume passages; absence findings cite the JD requirement and completed document scope; relational findings cite both affected spans; visual findings cite page/region. Never fabricate a quote for text that is missing. No culture-fit score, AI-authorship detector, mandatory verb rotation or forced metric count.

The separate reader pass is isolated from writer rationales, not a statistically independent oracle. Validate it with blinded human comparisons. Reviewers should identify lost accomplishment detail as well as gains in requirement presentation. Prioritize the top three useful actions instead of demanding that every advisory finding be resolved.

A stack of tools can be coherent when it explains one actual system; keyword overlap is not itself stuffing. Conversely, an abstract string of JD capabilities without a concrete action should not pass merely because each word is true somewhere in the candidate's profile.

### 7. Application criteria and overall status

Show known eligibility criteria separately using existing canonical matching/eligibility services. Use `met`, `potential_conflict`, `unknown`, `not_applicable`; do not infer work authorization or licenses from names or vague biography. A JD requirement alone is not proof of an employer-configured knockout rule.

Replace the current broad `ready` boolean with explicit statuses: analysis complete/partial, export checks, claim-check status, reader findings and automated-use eligibility. Users may review/download drafts despite warnings. Automated submission preparation retains its existing authorization gates and must not interpret “reader completed” as application approval.

## Version and ownership architecture

One async assessment service handles initial tailoring, regeneration, manual-edit rechecks, standalone rescoring and saved-file checks. Route names may remain backward-compatible; measurement logic must not branch based only on a client-supplied marker.

Resolve the owned record/source/JD snapshot on the server. A guest uses the existing guest-session ownership boundary. Client requests identify a document and revision; they do not supply authoritative provenance or a trusted assessment. Detached drafts can be assessed with explicitly unresolved source status.

Persist a versioned assessment with `assessment_id`, record/version ID, source/evidence hash, JD/requirement hash, content hash, file format/byte hash, renderer/extractor versions, policy/rubric versions, model/prompt versions, completion states, per-requirement observations and quoted findings. Attach it atomically only if the expected revision still matches.

Cache semantic review by ordered document blocks and entity relationships, canonical JD and rubric/model versions. Normalization must preserve word boundaries and grouping. Cache file checks by exact bytes and extractor/rule versions. Byte-only PDF timestamp changes should not spend another reader call. Renderer changes invalidate file checks; JD changes invalidate coverage/review; substantive edits invalidate affected claim and reader checks. Reuse unaffected scoped checks with explicit dependency tracking.

Rechecking must be read-only with respect to resume prose. A separate user-authorized refinement can act on findings. Any resulting substantive edit invalidates the affected checks and triggers a new assessment.

Initial assessment budget: one batched document-observation pass, one batched entailment pass for relationships not already validated, and one separate reader pass. These are proposed call limits, not measured latency guarantees. Oversized inputs use bounded whole-record chunks and partial results. Debounce edits, cache unaffected checks, and reserve worker time to persist partial completion. Writing repairs remain in the tailoring workflow's one bounded repair pass.

## Implementation order and files

| Phase | Work | Main files |
|---|---|---|
| 1: contract and unsafe defaults | Introduce a discriminated assessment contract; stop producing simulated vendor/culture/screening scores for new assessments; preserve historical records as labeled legacy estimates; missing inputs remain unknown. | `resume_scorer.py`, `schemas/resume_schemas.py`, `schemas/tailoring_contracts.py`, frontend `types/resume.ts`, `lib/atsScores.ts` |
| 2: scoring lifecycle | Server-owned assessment IDs, stale revision rejection, changed-claim and context revalidation, completeness-aware statuses and async integration. | `resume_service.py`, resume blueprint, `resume_versioning.py`, `tailoring_pipeline.py` |
| 3: actual artifact assessment | Return an ordered parsed document with structural associations; integrate renderer loss outcomes; separate PDF/DOCX checks and cache dependencies. | `pdf_ats_check.py`, `tailoring_assessment.py`, `resume_renderer.py`, download handlers; retire unsupported vendor assertions in `ats_vendor_checks.py` |
| 4: authoritative requirement evaluation | Share canonical requirement expressions and source IDs, add isolated final-document observations and source reconciliation, action/actor/qualifier-aware evaluation, fixed comparison groups. | `tailoring_planner.py`, `tailoring_assessment.py`; new `requirement_assessment.py`; reuse `keyword_normalizer.py` without frequency rewards |
| 5: useful interface | Requirement counts, separately labeled coverage/editorial changes, top actions, reader findings and actual-file status; keep visibility percentages internal until validated. | `DocumentAssessmentPanel.tsx`, `AtsScorePanel.tsx`, `TailorPage.tsx`, `TailoredPage.tsx`, `NudgeBar.tsx`, tracker/API types |
| 6: calibration and rollout | Offline adversarial fixtures, live consented/synthetic writing evaluation, blinded human comparison, latency/cost measurement, controlled flag rollout. | Existing scorer, evidence-tailoring, layout and round-trip tests/evals; implementation report |

Leave `fit_score.py`'s candidate/job formula unchanged. Reuse its canonical distinctions where compatible; do not make two competing match formulas with different names in the UI.

## Acceptance tests and release criteria

- Duplicating a keyword, adding its expansion, repeating an accomplishment or copying JD text cannot increase requirement presentation credit.
- Semantic reframing that makes an actual accomplishment clear can increase presentation credit without requiring new facts.
- If both versions already demonstrate the same capability, semantic coverage remains equal; cosmetic changes do not create a gain.
- Exploration, third-party exposure, ownership, historical use and explicit production experience remain distinct.
- Mixed AND/OR expressions retain grouping; aggregate duration does not double-count overlaps or assume tool use across an entire tenure.
- A longer, less specific resume cannot be preferred solely for covering more low-priority requirements.
- Missing-requirement findings do not require a fabricated resume quote; incomplete extraction prevents unsupported absence conclusions.
- Adding a skill only to Skills cannot satisfy a requirement for demonstrated use at an employer.
- Removing optional summaries/projects or using valid qualitative outcomes does not trigger an automatic penalty.
- A source-supported item lost during export is hidden/missing in document coverage and a file issue, never a visible match.
- Reordered employer/date associations and joined-word corruption are detected even when all substrings exist.
- A failed reviewer/extractor produces unknown/incomplete statuses, not zero ability, a default 70 or format 95.
- Unmet genuine requirements remain visible even if every supported requirement is well presented.
- Eligibility uncertainty does not become a fabricated conflict; known conflicts cannot be cured by keyword insertion.
- Manual edits can be rechecked without rewriting; validated semantic edits do not remain permanently stale.
- Assessments cannot be attached to a different user's record, a different revision, another JD or another export format. Forged client sidecars do not establish trust.
- A fresh backend deployment/rubric change invalidates the appropriate caches without modifying historical documents.
- Comparisons use the same frozen source, JD and rubric; otherwise show “not directly comparable.” Newly confirmed experience is labeled as new source evidence, not pure wording improvement.

Offline tests establish these invariants. Establish prose-quality and latency thresholds using representative multi-profession live samples and blinded human review before rollout. Track unsupported-claim errors, false hidden-evidence warnings, naturalness preference, parser disagreement, missing-data rate, latency and cost. Do not claim a pass probability from these tests or from observational interview rates.

## Example user-facing result

> File checks: PDF text and job/date associations checked; no issues found.
>
> Required requirements: 6 clearly demonstrated, 1 mentioned only, 1 related, 2 not evidenced in your source.
>
> What improved: your relevant payment-recovery accomplishment is now easier to find. Its factual coverage is unchanged.
>
> Reader feedback: the target role is clear; one bullet repeats an earlier point.
>
> Suggested change: make your existing payment-recovery work clearer in its current role. Kafka is currently listed as a skill only; add work context if you actually used it.

These example counts illustrate the interface, not an assessment of a real candidate. Keep the candidate's existing job-match result adjacent where useful, clearly labeled and unchanged by cosmetic edits.
