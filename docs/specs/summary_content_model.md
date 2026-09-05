# Summary content model

Companion to `summary_architecture.md`. That document defines the *pipeline*.
This one defines **what the summary should say**, and is the content contract
for the generation prompt.

Implemented by `services.summary_tailor` (`_build_generation_prompt`,
`allocate_tech_slots`, `validate_summary`).

---

## What a summary is for

Three jobs, in order:

1. **Placement** — tell the reader what kind of professional this is, in the
   first eight words.
2. **Capability** — what they build, operate, or deliver, with enough
   specificity that a hiring manager believes it.
3. **Differentiation** — optional. The thing that separates them for *this*
   job, when the brief has a supported, JD-relevant strength.

Three jobs it is **not** for:

- Stating what the candidate wants.
- Listing the full stack. That is the skills section.
- Restating the job description.

---

## The two- or three-slot structure

Total: 2–3 sentences, 45–70 words. The brief chooses whether sentence 3
exists (`include_s3`). The same word band is used in `prompt_rules`, the
validator, and this document.

### Sentence 1 — IDENTITY

```
[resolved role label] + [years, when honest] + [what they build or deliver]
+ [0–2 core languages, when relevant] + [domain qualifier, trailing, only if
the resume and JD share that domain]
```

- Role label comes from the brief — never copied blind from the JD title.
- Domain is a trailing qualifier, never the head noun.

### Sentence 2 — CAPABILITY

```
[delivery verb] + [JD-ranked evidenced systems, tools, or capabilities]
```

- Group work as one coherent story, not a comma list.
- Present tense — "Delivers", "Designs", "Builds", "Owns".

### Sentence 3 — DIFFERENTIATOR (optional)

```
[a JD-relevant supported strength] + [why it is credible]
```

- Reliability, product delivery, testing, scale, or another evidenced
  capability — not a hardcoded specialty.
- Omit it when the brief has no supported differentiator.

---

## Reference

The structure, annotated. This is a documented example, not a runtime
string and not a default narrative for other users:

> **[S1 · IDENTITY]** Backend and platform engineer with nearly 4 years
> building distributed, event-driven systems in Python and Java across
> regulated healthcare platforms.
> **[S2 · CAPABILITY]** Delivers high-throughput FastAPI and Spring Boot
> services with Kafka pipelines, PostgreSQL tuning, and Kubernetes deployment
> on AWS, instrumented with OpenTelemetry and Datadog.
> **[S3 · DIFFERENTIATOR]** Builds RAG and LLM capabilities into production
> workflows, applying the same backend and observability discipline to GenAI
> systems.

Every clause is checkable against a bullet below it. Another candidate with
different evidence must not inherit this industry, stack, or specialty.

---

## Technology and fact budget

Selection is JD-ranked and evidenced:

1. Terms in `selected_terms` (required/matched JD skills that appear in a bullet)
2. Other evidenced JD keywords
3. Other evidenced terms by bullet weight

Base-summary vocabulary is a candidate pool, not a mandatory prefix.
Validation does not reject a draft for omitting an irrelevant base term.

A technology named in the summary **must appear in a bullet below**. No
exceptions — including JD keyword-budget terms.

Engineering profiles use a small relevance floor (up to 3 named
technologies when the brief allocated them) and a ceiling of 11 to stop
keyword dumps. Non-engineering profiles have no technology quota. A
sentence may be evidenced by a supported capability or metric without
naming a framework.

---

## What tailoring may and may not change

| Element | Tailorable |
|---|---|
| Which evidenced facts fill S2 and S3 | **Yes** |
| Ordering within a sentence | **Yes** |
| Whether S3 exists, and which strength it carries | **Yes** |
| Domain qualifier | **Yes** — only when resume and JD share it |
| Role label in S1 | **Only within the role policy** |
| Years, employers, metrics | **No** |
| Invented skills, seniority, or specialist identity | **No** |

---

## Generation prompt rules

Positive requirements — every one must hold:

1. Exactly 2 or 3 sentences, 45–70 words.
2. S1 opens with the resolved `role_family` as the grammatical subject.
3. Every sentence contains a selected term, keyword-budget term,
   prioritized capability, or numeric metric.
4. Named technologies are backed by a bullet.
5. Present tense, first-person-implied, no "I".
6. Domain qualifier trailing in S1 when present, never the head noun.
7. Every sentence ends with a period.

Banned outright:

- Objective phrasing: `ready to`, `seeking`, `looking to`, `eager to`,
  `hoping to`, `excited to`, `passionate about`, `aiming to`,
  `bring this foundation`
- Adjective openers: `Results-driven`, `Highly skilled`,
  `Detail-oriented`, `Motivated`
- Self-assessment: `expert`, `world-class`, `extensive experience`,
  `proven track record`
- Comma-list inventory in place of a system story
- Any technology absent from the bullets below

---

## Tests

Locked in `tests/test_summary_tailor.py` and
`tests/fixtures/summary_role_matrix.py`:

1. Broad SWE vs specialist identity.
2. JD-ranked fact selection; base-only terms are not mandatory.
3. Project bullets can back a required term.
4. Nontechnical profiles validate without a technology quota.
5. Valid drafts ship even when the informational score is below the base.
6. Fallback sidecar reason codes; refresh after augmentation.
7. No assumptions imported from another profile.
