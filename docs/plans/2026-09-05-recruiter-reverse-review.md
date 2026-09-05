# Recruiter-side reverse review of the tailoring architecture

Status: analytical review and illustrative thought experiment, 2026-09-05. This is a recruiter-perspective rubric, not a claim of personal recruiting employment, a live ATS test, or a completed human evaluation.

## Verdict

The architecture is directionally sound for revealing existing fit. It should help when a qualified person's accomplishments are poorly described, buried, or expressed differently from a job posting. It cannot establish missing qualifications, recover unknown employer criteria, or guarantee a shortlist. Its effectiveness is not demonstrated until real outputs are compared.

The remaining weakness is selection judgment: the plan has strong factual safeguards but needs a clearer model of the hiring decision. A truthful, readable resume can still be generic, too junior for the role, or less convincing than competing applications. Optimize for a recognizable reason to interview, not only an absence of defects.

## Reverse the decision, not a proprietary algorithm

These are useful review questions, not a universal sequence used by every employer:

| Decision | What the employer needs to establish | What tailoring can accomplish |
|---|---|---|
| Application eligibility | Required credentials or answers to explicit application conditions. | Clarify known facts; flag unknowns. Resume wording cannot repair an ineligible answer. |
| Search/review visibility | Relevant qualifications are recognizable in the document/application. | Use accurate terminology in identifiable context and a readable export. |
| Initial shortlist | The person's recent/strongest work fits the role and level. | Foreground the best examples and make actual contribution/scope clear. |
| Hiring-manager interest | The work demonstrates useful depth, judgment, and a reason to prefer this applicant. | Preserve distinctive problems, actions, constraints, and supported results rather than replacing them with abstractions. |
| Interview consistency | The person can explain what the resume claims. | Keep ownership, scope, tools, and outcomes consistent with the source account. |

Product evidence supports separating these questions. Workable documents configurable automatic disqualification based on application answers. [Workable](https://help.workable.com/hc/en-us/articles/115012238688-Auto-disqualify-candidates-using-application-form-questions)

Greenhouse Talent Matching uses employer-defined, weighted criteria and highlights matching experience/skills; the feature does not automatically advance or reject candidates. Its calibration can use information beyond the public JD. We cannot reproduce a particular employer's settings from a pasted posting. [Greenhouse](https://support.greenhouse.io/hc/en-us/articles/41396009937307-Talent-Matching)

Ashby supports job-specific criteria, including criteria suggested from the JD or reused from other jobs. This supports requirement mapping, not a universal keyword quota. [Ashby](https://www.ashbyhq.com/product-updates/automatically-generate-ai-job-criteria)

## What the plan gets right

- Separates qualification evidence from presentation and file readability.
- Allows semantic reframing rather than requiring verbatim source phrases.
- Retains strong original writing and removes keyword/repetition incentives.
- Preserves user edits and connects final assessments to the exported version.
- Treats incomplete information as unknown rather than inventing confidence.

## Changes needed before implementation

### 1. Create a hiring-decision brief before selecting bullets

Summarize the role's main work, expected level/scope, and a small set of decisive requirements. Explicit JD requirements and inferred priorities must remain distinct; these are hypotheses about employer priorities, not knowledge of its private scorecard. Start with roughly three to five decision drivers as a planning heuristic, not a resume keyword quota or fixed rule.

For each driver, select the strongest available example and identify what it does and does not establish. The internal candidate case should answer: "Why is this person worth discussing for this particular role?" It must cite existing facts and acknowledge a major unresolved gap. Do not print this internal argument verbatim into the resume.

### 2. Model depth, not only whether a capability appears

Add evidence context: recency, work/project/course setting, personal contribution, ownership, scale, frequency, and outcomes when stated. One retry implementation demonstrates failure-recovery work; it does not by itself demonstrate ownership of a reliability program. One campaign contribution does not establish marketing strategy leadership.

Absent depth is unknown, not zero. Do not manufacture measurements or disqualify users solely because the source omits numbers. Source context should guide which claims deserve top placement and which remain supporting details.

### 3. Separate factual review from reader-visible review

The factual validator may see the source snapshot and references. The recruiter-facing evaluator must see only the final rendered resume and JD, not fact-store notes, coverage mappings, previous scores, or the writer's reasoning. Otherwise it can reward evidence invisible to the employer.

Ask the reader-only evaluator for the apparent role/level, strongest visible reasons to interview, unresolved concerns, and exact resume passages supporting its conclusions. No universal six-second cutoff or invented interview probability. Reconcile a missing visible fact by improving placement/wording, not by teaching the evaluator to assume it.

### 4. Preserve specificity and avoid semantic inflation

The planned example "Delivered a checkout feature" is potentially stronger than source "Worked with designers and QA to release a checkout feature." Safer equivalent: "Worked with design and QA to release a checkout feature." A cross-functional label may be added when useful, but it need not replace the concrete collaborators.

Use the most informative wording, not the most abstract JD phrase. Also preserve comparator semantics precisely: `2M+` normally supports "at least 2 million"; do not automatically strengthen it to strictly "more than 2 million". Unknown comparator intent should preserve the source form.

### 5. Prioritize a credible story over perfect coverage

Make a few strong, relevant accomplishments easy to find early, appropriate to the person's career stage. Preserve distinctive achievements even when they do not match exact JD vocabulary. Do not make every past role sound identical to the target role. The remaining skills and experience supply breadth.

Keyword presence and capability depth are separate findings. A tool accurately listed only in Skills is findable but has limited usage context; do not treat it as worthless and force a fabricated bullet. Conversely, repetition cannot turn one narrow example into deep experience.

### 6. Keep the first release smaller

Retain one orchestrator and clear contracts, but do not equate every logical stage with another model call. Start with selection/decision brief, one scoped rewrite, final factual validation, one independent reader review, and actual export checks. Use bounded targeted repair. Share structured inputs and existing infrastructure.

Measure latency, cost, and user edit burden. A theoretically exhaustive pipeline that repeatedly rewrites good text or waits on optional evidence questions would fail the product goal.

## Illustrative reverse test

Assumed source facts: a candidate built Java services, added job retries, and used SQL indexes to reduce report queries from eight seconds to two. Nothing establishes team leadership or incident-program ownership. This is hypothetical input, not the user's verified history.

| Version/target | Reader-side assessment |
|---|---|
| Keyword-heavy: "Java, distributed systems, reliability, scalability, SQL, performance optimization." | Tools and capability labels are visible, but contribution and depth are unclear. Poor human evidence. |
| Natural: "Added retry handling to recover failed background jobs. Optimized report queries with SQL indexes, reducing execution time from eight seconds to two." | Clear actions and outcome. Useful evidence for a relevant software role without decorative claims. |
| Same natural version for a senior SRE role requiring incident leadership | Still lacks visible leadership/reliability scope. Better prose has not solved the substantive mismatch. |
| Same facts buried below generic responsibilities | The evidence exists but presentation is weak. Selection/order can improve this without adding facts. |

Conclusion from this thought experiment: the mechanism can expose fit, but adequacy depends on the role's required depth. This is not evidence of improved callback rates.

## How to establish whether it works

Compare three versions: original, current pipeline output, and proposed pipeline output. Use the same source/JD inputs. Include cases with direct fit but poor wording, adjacent fit, genuine major gaps, already-strong resumes, and nontechnical professions.

Have independent human reviewers use a job-specific rubric without knowing which system wrote each version. Randomize order. First review the final document and JD only; then separately audit fidelity against source facts. Capture reasons to advance/hold/decline, visible scope, clarity, unsupported inferences, and reader disagreements. A model reviewer is additional evidence, not a substitute for these comparisons.

Success means readers can identify more of the candidate's real relevant strengths, with fewer unsupported interpretations and no loss of credibility. On major-gap cases, success is honest clarity, not manufacturing an advance decision. Also measure keyword findability, export retention, turnaround time, and user correction burden. Set acceptance thresholds before examining evaluation results; do not invent a universal ATS pass score.

Recommended decision: proceed with a measured first implementation after incorporating these corrections. Do not market the plan itself as proof that applications will pass screening.
