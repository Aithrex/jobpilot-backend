# Evidence-led resume tailoring for ATS and human review

Implementation contract: [unified architecture and workflow](../specs/evidence-led-tailoring-architecture.md), with a [current conflict audit](2026-09-05-tailoring-conflict-audit.md). Use those documents for current outstanding work, stage ownership, failure handling, and migration. Summary improvements landed locally during the later audit; summary-specific findings below describe the earlier baseline.

Status: proposed architecture, based on local code review. No runtime changes or live generation tests performed.

This is the umbrella plan for the entire resume. The [summary plan](2026-09-05-jd-adaptive-summary.md) is a subordinate workstream; its ordering and evidence rules must follow this pipeline. Scope covers all users and professions, not a particular resume or technical stack.

## Product objective

Naturalness requirement: strong JD alignment must preserve concrete accomplishments, appropriate voice, and clear wording. The architecture's naturalness contract removes forced keyword placement, all-bullet rewriting, metric quotas, and mechanical verb variety; its final review rejects unnecessary keyword clauses without undoing valid semantic reframing.

Approved refinement: allow substantial supported reframing and targeted discovery of real experience missing from the resume. See architecture Stage 2a for the transformation policy, optional questions, confirmed additions, examples, and acceptance criteria. Tailoring should surface credible fit, not merely preserve wording or insert keywords.

Clarification: evidence is primarily interpreted internally. The writer automatically names capabilities demonstrated by the user's actions, even when those capability words are absent from the resume. Valid semantic support is accepted without questions. Ask only for optional material new facts; repair uncertain clauses locally and retain the rest of the tailored wording. The architecture now requires an adaptation review to catch overly conservative outputs as well as overstatement.

Produce a truthful resume whose relevant qualifications are easy for recruiting systems to find and easy for a person to understand. Optimize three distinct things: document readability by software, evidence of job requirements, and persuasive human presentation. A high keyword count is insufficient evidence of any hiring outcome.

ATS products and employer configurations vary. Our scores are internal diagnostics, not measured probabilities of passing Workday, Greenhouse, or an employer's screening. Application questions and genuine qualification gaps must remain separate from resume wording.

## Current flow and useful foundations

The service extracts JD fields, selects a base resume, builds confirmed profile facts and keyword gaps, and invokes primary tailoring. Integrity enforcement and an optional quality reflection happen before skill restoration, bullet allocation, and the deferred summary writer. Service finalization then runs augmentation, normalization, advisory verification, and a final keyword audit. Rendering and the requested scoring flow can check the generated PDF's extracted text. Regeneration and other entry points have related paths that need contract coverage.

Keep the existing fact store, integrity guard, alias normalization, base selection transparency, verification sidecars, version history, and PDF round-trip tests. Extend the multi-profession evaluation harness rather than starting another isolated evaluation system.

## Findings from the current implementation

| Area | Observed behavior | Implication |
|---|---|---|
| JD interpretation | `extract_jd` reads the first 5,000 characters and extracts flat skill and responsibility lists. | Requirements later in long JDs can be missed; source spans, alternatives, and qualification categories are not explicit in this extraction contract. |
| Keyword strategy | `ResumeTailor._build_keyword_context` targets 80–90% required coverage and asks missing terms to be woven into adjacent work. Its example treats any HTTP service as evidence for REST APIs. | A coverage target can pressure the model to overstate related experience. Adjacent work is not necessarily equivalent work. |
| Skills | `_ensure_jd_skills_coverage` restores base skills and tests additions against original plus tailored text or skill implications. | Relevant pruning is limited, and model-written text participates in deciding whether another addition is supported. Global skill ownership does not establish use at a particular employer. |
| Late keyword changes | `ContentAugmenter._enforce_keyword_density` targets skills-only and single-occurrence terms when contextual coverage is below 80%, using the first five experience bullets. | It can introduce contextual claims without the source evidence linking the skill to that role. It selects by position rather than the best evidence. |
| Page filling | Augmentation can generate project content, expand bullets, inject impact wording, and repeat expansion based on fill. | More text may increase density while reducing relevance. Every added accomplishment needs provenance, independent of spare page space. This review does not establish that all generated projects are fabricated. |
| Human quality | The optional reflection gate checks an intermediate draft. Its judge receives the draft and JD but not the original resume, despite a metric-preservation dimension. The repaired candidate is integrity-checked but not compared again for human quality. | The judge cannot establish source-metric preservation from those inputs, and later changes can undo the quality improvement. |
| Scoring incentives | `ResumeScorer._combine` gives 25% to keyword match, 5% to frequency, and 20% to skills alignment. Frequency rewards repeated terms. | Half the composite weight is tied to overlapping lexical/skill signals, encouraging score chasing. |
| Scoring credibility | `_ai_scores` asks the model to simulate named ATS vendors and screening likelihood. Deterministic format score starts at 95. PDF checks separately cap the overall score when bytes are provided. | Vendor scores are simulations, not vendor test results. Real PDF checks already exist and should replace format assumptions; a missing check must not imply success. |
| Final validation | Service-level verification is advisory after augmentation. Summary generation happens before those edits. | Final output can differ from the content that passed earlier checks. Late changes need the same grounding standard. |

The exact deployed configuration and rates of these failures remain unverified. Record the runtime revision, enabled reflection setting, generation stages, and fallback reasons before attributing a production symptom to a specific branch.

## Proposed pipeline

JD requirement map + source-backed candidate facts → requirement-to-evidence mapping → content selection → constrained rewriting → factual validation → human quality review → final summary → render and extract → final checks → versioned result.

### 1. Interpret requirements before selecting keywords

Create a `JobRequirements` contract with stable IDs, source excerpts, requirement type, required/preferred status, priority, and confidence. Distinguish responsibilities, tools/methods, domain experience, seniority/scope, credentials, and application logistics. Preserve "A or B" alternatives and distinguish explicit must-haves from model-inferred importance.

Replace silent prefix truncation with bounded section-aware processing and an explicit incomplete-analysis status. Do not treat employer marketing copy as a requirement. Reuse the current extraction call where practical; add chunking only when needed.

### 2. Match requirements to evidence, not just text

Extend `profile_facts` with source IDs and scope: employer/project, dates, action, tools/methods, ownership, and outcomes. Preserve the user's original facts independently of all generated versions. Tailored text must never become new grounding by itself.

Each requirement gets one of these states:

- **Demonstrated:** a source accomplishment supports the capability in context.
- **Listed or confirmed:** the user has stated the skill, but its employment/project context is unknown.
- **Related:** transferable experience exists, but equivalence is not established.
- **Not evidenced:** no supporting fact was found.
- **Needs clarification:** source information conflicts or is incomplete.

Aliases such as PostgreSQL/Postgres may normalize safely. Related tools or methods remain related; do not silently turn them into exact experience. A listed skill may stay in Skills without being inserted into a work bullet. Use existing confirmed-enrichment flows for optional, targeted questions when an important missing fact would improve the application; do not block routine tailoring.

### 3. Select the strongest content before rewriting

Build a `TailoringPlan` that assigns each important, supportable requirement to the best evidence. Rank by relevance, evidence strength, specificity, and recency; reduce redundancy across bullets. Prefer distinct accomplishments rather than several bullets proving the same skill.

Keep employment chronology and actual titles intact. Reorder bullets within roles, prioritize relevant real projects and credentials, and allocate space according to career stage and evidence. Preserve the full source resume while allowing low-value content to be omitted from this application, respecting user-pinned content. Do not require every source skill or number to appear in every tailored version; preserve the exact meaning and numbers of retained claims.

### 4. Rewrite accomplishments with bounded changes

Give the writer the selected source facts, requirement IDs, and allowed terminology for each bullet. Each bullet should communicate a clear action, the work or problem, and a supported result or scope when available. Numbers are valuable when real; specific qualitative outcomes are valid too.

Use an exact JD term when it accurately describes the source work and improves clarity. Prefer one coherent accomplishment per bullet. Do not append unrelated tools, manufacture results, inflate ownership, or repeat a keyword to satisfy a quota. An already strong bullet can remain unchanged.

Replace the later keyword-density pass with targeted repair of a demonstrated requirement that was accidentally lost in wording. Repairs must reference the relevant source fact and role. No fixed repetition quota and no universal 80–90% target. Cover all relevant supported requirements where space permits; disclose the rest honestly.

### 5. Apply separate factual and human checks

Run deterministic factual checks after every semantic mutation. Verify new claims against source facts, including tool-to-role association, ownership, dates, metrics, credentials, and project existence. Unsupported additions are removed or reverted. A model audit failure means unverified, not supported; retain the last verified text where available.

Run a human-quality reviewer on the settled content with source evidence and JD priorities. Evaluate relevance of the leading content, understandable accomplishments, specificity, redundancy, unexplained jargon, overlong bullets, and coherent career positioning. Return cited issues and bounded repairs, not just a number. Compare repaired content with the prior draft using the same rubric; reject a repair that gains keyword coverage by losing clarity or introducing unsupported claims.

Write the summary from the resulting selected evidence, then validate the complete document. Grounded expansion should happen before this review. Cosmetic formatting afterward must not introduce semantic changes; any semantic trimming or rewrite returns through validation.

### 6. Fit the document to the evidence

Replace page-fill-driven content creation with relevance-driven selection. Allow whitespace or an additional page when justified. Preserve readable type, clear headings, and reliable chronology. Real projects may be rewritten from documented evidence; proposed projects belong outside the application resume until completed and confirmed.

Reuse `pdf_ats_check` and layout tests to compare final extracted text with structured content: contact details, job/company/date associations, section order, retained bullets, metrics, and critical terms. Add export-specific DOCX checks where offered. Validate the actual final artifact, including after user edits; tie diagnostics to the document version/hash. A local extraction check is evidence of extractability, not proof of a particular vendor's behavior.

### 7. Replace the single score with useful feedback

Show three separate panels:

| Panel | What the user learns |
|---|---|
| Document readability | Whether the exported file's text, sections, and core fields survived extraction; explicit unchecked/error state. |
| Job requirement coverage | Which requirements are demonstrated, listed, related, or not evidenced, with supporting resume locations. |
| Recruiter review | Whether the opening content establishes fit and the bullets explain credible accomplishments clearly. |

Show qualifications/application constraints separately; wording cannot fix an absent license or an unanswered eligibility question. Replace named-vendor simulated scores and inferred screening/culture-fit probabilities with these observable findings. If a composite remains temporarily for compatibility, label it an internal estimate and remove rewards for bare repetition. Unknown checks must not receive optimistic defaults.

Provide a concise change explanation: what was emphasized, which facts support it, what was omitted for relevance, and which gaps remain. Users should not need to interpret internal stage logs.

## Validation and rollout

1. **Baseline:** extend existing multi-profession fixtures and capture current pipeline outputs, coverage states, keyword repetition, readability, unsupported claims, latency, and fallback rates. Include a long JD with a late requirement and profiles with no technologies or numerical outcomes.
2. **Evidence foundation:** add requirement and fact references; test listed-only skills, adjacent tools, role association, alternative requirements, and confirmed enrichment. Ensure no cross-user evidence mixing.
3. **Rewrite path:** replace quota-driven keyword insertion and fill-driven additions; test relevance selection, unchanged strong bullets, truthful gaps, exact metric retention for selected claims, and late-mutation checks.
4. **Final review and export:** test all tailoring entry points, regeneration, edits, persistence, previews, and exports against one document version. Reuse existing layout and grounding tests.
5. **Quality evaluation:** compare current and proposed outputs blindly across multiple profiles and JDs, using both a rubric-based model judge and consenting human reviewers. Stubbed tests establish contracts, not writing quality. Require no unsupported additions in the regression set, critical content retention in exports, and no increased score from duplicating keywords. Calibrate human quality thresholds from the baseline rather than inventing a pass percentage.
6. **Rollout:** release behind a versioned pipeline flag, compare diagnostics, monitor cost/latency and failures, and retain rollback. Update schemas, UI labels, prompts, and architecture documents together. Application outcomes may inform later evaluation with user consent, but are confounded and must not be presented as proof that one score guarantees interviews.

## Primary implementation areas

- `resume_service.py`, `resume_schemas.py`: JD extraction contract, orchestration, final-version diagnostics.
- `profile_facts.py`, `keyword_gap_engine.py`: evidence references and requirement states; reuse normalization.
- `resume_tailor.py`, `prompt_rules.py`: content selection, constrained rewriting, final quality comparison.
- `content_augmenter.py`, `project_generator.py`: remove quota/fill incentives; enforce source-backed additions.
- `summary_tailor.py`: apply the linked summary workstream after content settles.
- `resume_scorer.py`, `pdf_ats_check.py`, `tailor_verifier.py`: separate coverage, factuality, readability, and observed export checks.
- Frontend `AtsScorePanel`, `ReviewPanel`, and version/change views: explain evidence and gaps instead of simulated vendor outcomes.

## External basis

Workable describes keyword highlighting in candidate information, while its resume guidance explicitly recommends readable, honest writing and avoiding keyword stuffing. These support natural terminology and clear accomplishments, not a repetition quota. Sources: [keyword matching](https://help.workable.com/hc/en-us/articles/115012207128-Adding-keywords-to-a-job), [resume guidance](https://resources.workable.com/career-center/how-to-optimize-your-resume-for-an-ats/).

Greenhouse documents partial and failed parsing from image-based resumes, complex formatting, columns, and unclear sections. This supports testing exported documents. It does not establish a universal ATS pass score or that a local parser reproduces Greenhouse. Source: [unsuccessful resume parsing](https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse).
