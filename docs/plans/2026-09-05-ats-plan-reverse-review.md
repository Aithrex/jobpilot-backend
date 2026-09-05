# Reverse review of the ATS measurement plan

The corrections here are consolidated into the [ATS engine master implementation plan](2026-09-05-ats-engine-master-implementation-plan.md).

Date: 2026-09-05. Scope: adversarial design review of `2026-09-05-ats-measurement-audit-and-plan.md`, with targeted deterministic probes of the current implementation. No production code changed. These scenarios are design counterexamples, not live ATS trials or recruiter-study results.

## Verdict

The separation of parsing, requirement presentation, factual support and reader quality is sound. The plan is not ready to implement unchanged. Its main remaining risk is replacing keyword optimization with coverage optimization: a writer could include every supported requirement, earn good counts, and still produce an exhausting, unnatural resume.

The revised design should optimize useful presentation under a realistic page/attention budget, recognize when the original is already good, and show concrete improvements or unresolved issues. Do not launch the proposed visibility percentage until it has been shown to measure something stable and distinct from semantic coverage.

## Work backward from the intended outcome

For a useful result, all of the following must hold:

1. The exact submitted file is readable and preserves the intended relationships.
2. A relevant requirement is actually expressed, with the correct action, actor and qualifications.
3. That expression is backed by the candidate's stated source experience at the right scope.
4. Important relevant work is reasonably easy to find and understand.
5. Tailoring has not displaced stronger evidence, inflated ownership, or made the document needlessly repetitive.

No one count proves all five. Some improvements are measurable as changed coverage; others are editorial improvements without changed coverage. A good unchanged sentence is a successful outcome, not a tailoring failure.

## Failure cases and required corrections

| Priority | Attack / counterexample | Why the earlier plan is insufficient | Revision |
|---|---|---|---|
| P0 | The source says “added retries for failed requests”; the rewrite says “improved failure recovery using retries.” | Both demonstrate recovery semantically. Calling the original hidden simply because it lacks a capability label creates an artificial improvement. | Assess both documents with the same semantic rules. Coverage stays equal; only a separately justified clarity/discoverability finding can improve. |
| P0 | A candidate supports only one of ten requirements and presents that one clearly. | A 1/1 or 100% visibility badge is easy to misread as strong fit even with caveats. | Do not ship a standalone visibility percentage initially. Show requirement states and changes; keep candidate/job matching separate. |
| P0 | Include every supported capability across a longer summary, skills list and many bullets. | Deduplicating coverage stops repetition credit but does not stop coverage-driven bloat. | Treat document length/readability constraints and retained concrete accomplishments as acceptance criteria. Missing low-priority coverage can be an intentional tradeoff. |
| P0 | “Evaluated Kafka but did not deploy it” satisfies “production Kafka experience.” | Matching the technology does not satisfy the action or production qualifier. | Evaluate predicates: action, actor, use context, ownership, duration, and negation. Only demanded qualifiers affect satisfaction. Exploration still counts accurately as exploration. |
| P0 | “Worked with a platform team's Kafka service” becomes “owned Kafka infrastructure.” | Related exposure can be confused with ownership even with an exact quoted source. | Quote-backed support must establish the proposed claim, not just share its nouns. Preserve actor/contribution relationships. |
| P0 | Source supports an accomplishment, but tailoring moves it to a more recent employer. | Global source support and visible keywords can both pass. | Require source-to-document entity association. An unchanged bullet under a different employer is a substantive factual change. |
| P0 | “Python or Java, plus SQL” is flattened into loosely matched terms. | Counts depend on how extraction happened to split the sentence. | Represent explicit AND/OR groups and attach qualifiers to the correct node. Ambiguous wording remains ambiguous; do not invent an employer interpretation. |
| P0 | Reviewer must quote every finding, but the important fact is absent. | A missing license, hidden accomplishment or omitted requirement has no resume passage to quote. | Distinguish positive/critique findings from absence findings. An absence finding cites the JD requirement and a completed document search, with relevant examined sections, not a fabricated resume quote. |
| P1 | A generic junior bullet covers many broad requirements while one strong specialized accomplishment is removed. | Coverage can stay equal or rise while the resume loses a persuasive reason to interview. | Add accomplishment-retention and contribution-specificity review, with source-backed before/after examples. Do not score a resume solely by requirement counts. |
| P1 | An old project and recent employment both mention the required technology. | Binary support erases useful context; a universal age penalty would introduce another arbitrary rule. | Display when/where it was used and whether the JD explicitly demands recent use. Treat inferred recency preferences as advisory. |
| P1 | Two overlapping roles are used to satisfy three years of experience. | Several source references are valid but naive addition inflates duration. | Permit multi-fact support for aggregate requirements; union relevant date intervals, retain uncertain dates, and never infer continuous tool use from full tenure alone. |
| P1 | Every small source correction resets the benchmark and shows a new positive delta. | Frozen snapshots alone do not define which runs are comparable. | Track source, requirements and rubric lineage. When any changes, re-evaluate both documents against the new common basis or suppress the delta. Distinguish added evidence from wording changes. |
| P1 | One ambiguous requirement causes every figure to disappear. | Globally returning unknown for any incomplete analysis wastes reliable findings. | Preserve completed per-requirement findings with an explicit partial-analysis banner. Suppress aggregate percentages while still showing assessed counts and unresolved items. |
| P1 | Same text, different column order or job grouping, receives cached reader feedback. | A plain normalized-text cache can erase structure that matters. | Hash ordered semantic blocks and entity relationships. File checks remain byte-specific. Do not normalize away word boundaries or associations. |
| P1 | Same model writes and then “independently” reviews its own style. | Separate prompts reduce information leakage but do not establish statistically independent judgments. | Describe it as a separate reader pass. Validate against blinded human assessments and adversarial fixtures; disagreement is uncertainty, not a user failure. |
| P1 | The system marks a source-backed license statement “verified.” | A candidate's source assertion is not external license verification. | Use “supported by your source.” Reserve externally verified status for actual authoritative verification with provenance and time. |
| P1 | Strong required-term discoverability is dismissed because semantic coverage already passes. | Exact professional naming can still make a document easier to search or recognize. | Permit one useful, supported naming clarification. It receives a discoverability finding, not extra requirement coverage or repetition credit. |
| P1 | Validation creates multiple model calls per bullet and a whole-document pass after each edit. | The plan has no concrete cost/deadline controls and could become slow and inconsistent. | Batch scoped claims, cache unchanged source relationships, debounce edits, and bound evaluation/repair calls. Benchmark latency before rollout. |
| P2 | User sees four status cards and twenty concerns but cannot decide what to change. | Honest measurement can still be unusable. | Show a concise document status, top three actionable issues, what improved, and expandable requirement details. Do not require all boxes to be green. |

## Executed probes of current code

Using `tailoring_planner.build_plan` with a simple Kafka requirement:

- “Evaluated Kafka but did not deploy it.” -> `demonstrated`.
- “Integrated an API maintained by another team using Kafka.” -> `demonstrated`.
- “Built Kafka consumers for invoice processing.” -> `demonstrated`.

The requirement in this probe is only “Kafka,” so these outputs do not by themselves prove an incorrect decision for an explicit production requirement. They prove that the current state collapses materially different relationships into one label. The next contract must retain those relationships before attempting qualified matching.

The existing export-normalization predicate also returns true for normalized `SQL` inside normalized `NoSQL`. This is a concrete boundary weakness in substring-presence checks, not evidence that a real ATS confuses the terms.

For “Python or Java and SQL,” the current planner returns the flat alternatives `Python` and `Java and SQL`. This establishes the absence of an explicit Boolean tree; the sentence itself may be ambiguous and must not be silently resolved as the employer's intended grouping.

## Revised requirement contract

Represent a requirement as an expression of atomic predicates rather than one keyword string. Keep the original JD span alongside the expression.

An atomic predicate includes capability/tool, required action or relationship, explicit setting, ownership/level if stated, duration or currency if stated, and evidence expectation. Qualifiers not stated by the employer remain absent; do not demand production use for every tool in a generic Skills list.

Evaluation returns `satisfied`, `partially_supported`, `not_demonstrated`, `contradicted`, or `unknown`, with exact document spans, source references and unmet qualifiers. This is an internal requirement evaluation, not a second candidate-match score.

- AND: all required children must be satisfied for the group to be satisfied.
- OR: one complete qualifying branch can satisfy the group.
- Unknown: do not coerce unknown into a contradiction or a successful match.
- Aggregate: retain multiple explicit source relationships and calculate duration only when their evidence supports that calculation.
- Negation: distinguish never used, evaluated only, previously used, migrated away, and current use. A migration away may still demonstrate substantial historical experience.

A job-level group counts once in headline counts. Atomic details explain its result without inflating headline totals. Canonical grouping must be identical across the base/tailored comparison.

## Revised presentation of improvement

Use three explicit change types:

1. **Coverage gained/lost:** a requirement previously not demonstrated in the document becomes demonstrated, or vice versa, under the same evaluator and evidence basis.
2. **Clarity/discoverability improved:** the same supported capability is easier to find or understand. Show the actual changed passage and the reason; do not invent a coverage gain.
3. **New source evidence added:** the candidate supplied additional experience. Attribute the change to that addition, not merely to the writer.

Equal coverage with fewer repetitive sentences can be an improvement. More coverage with weaker accomplishment detail can be a regression. No change can be correct when the original is already strong.

The public interface should initially use concrete counts and quoted changes. Retain the proposed visibility ratio only as an experimental internal diagnostic. Public use requires evidence of stable judgments and user understanding; do not select an arbitrary small-denominator cutoff to make the metric appear rigorous.

## Reader findings that can actually be supported

Positive and wording-critique findings cite resume spans. Requirement-absence findings cite the JD requirement plus document scope/completeness, without pretending to quote missing text. Relational findings cite both affected sections/spans, such as an accomplishment attached to the wrong role. Visual findings cite page and region and the observed layout defect.

An absence finding is unavailable if the relevant document portion could not be parsed. A quote is a location reference, not proof that a model's conclusion is correct.

## Practical service boundary and budgets

The canonical assessment service owns artifact parsing, requirement observations, source reconciliation, reader findings and result persistence. Existing renderers, ownership services and `fit_score` remain separate dependencies.

Initial implementation budget: one batched document-requirement observation call, one batched source-entailment call for relationships not already validated, and one separate reader pass. This is a design cap for assessment, not a claimed measured latency. Large inputs use bounded whole-record chunks and explicitly partial completion. Do not make one request per keyword or bullet. Reuse existing valid tailoring checks only for unchanged, correctly scoped claims.

Automatic writing repairs stay in the tailoring workflow and are limited to the already planned single repair pass. A standalone recheck never rewrites prose. After an edit, recheck affected relationships and rerun document-level reader review when structure, positioning or context changes. Deletion of a role heading affects every bullet under it, even if their text is unchanged.

Choose wall-clock and token limits from staging measurements and the worker's configured deadline. Reserve time to return a partial result and persist it. Provider failure does not produce a default numeric score. Human users receive usable completed checks rather than waiting for all checks indefinitely.

## Release decision

Implement the corrected contract and adverse-case fixtures before adding public aggregates. Then implement authoritative export structure and assessment ownership, followed by semantic observation/reconciliation, and finally the UI and staged evaluation. Infrastructure for version binding must not be postponed until after scores are exposed.

Require zero fabricated full-credit matches on the deterministic adversarial suite, correct handling of unknown/partial states, and no cross-entity provenance acceptance. For subjective naturalness and presentation, collect blinded paired judgments, report agreement and disagreements, and establish release thresholds before evaluating the final holdout set. Do not claim empirical quality validation from this reverse review.

The design is suitable to proceed after these corrections. It still cannot promise ATS acceptance or recruiter selection; it can give a defensible account of what the final resume demonstrates and which changes are worth making.
