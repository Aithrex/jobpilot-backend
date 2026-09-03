# Summary content model

Companion to `summary_architecture.md`. That document defines the *pipeline* —
defer generation, build a deterministic brief, accept comparatively. This one
defines **what the summary should say**, and is the content contract for the
Phase 2 generation prompt.

Implemented by `services.summary_tailor` (`_build_generation_prompt`,
`allocate_tech_slots`, `validate_summary`).

---

## What a summary is for

Three jobs, in order:

1. **Placement** — tell the reader what kind of engineer this is, in the first
   eight words. This is the only sentence that assigns an identity; everything
   below the summary is evidence for it.
2. **Capability** — what they build and operate, with enough specificity that
   an engineering manager believes it.
3. **Differentiation** — the thing that separates them from other candidates
   with the same title.

Three jobs it is **not** for:

- Stating what the candidate wants. Applying already said that. An objective
  sentence spends 15% of the summary on zero information.
- Listing the full stack. That is the skills section. A summary holds 8–11
  technologies before it reads as a keyword dump.
- Restating the job description. A summary that mirrors the JD back tells the
  reader nothing they did not write themselves.

---

## The three-slot structure

Each sentence has one job and a technology budget. Total: 2–3 sentences,
45–70 words.

### Sentence 1 — IDENTITY

```
[role family] + [years] + [what they build, at the system level]
+ [1–2 core languages] + [domain qualifier, trailing]
```

- Role family is resolved by the bridge budget in the brief — never copied
  blind from the JD title.
- Languages only here. Not frameworks. The spine, not the stack.
- The domain goes at the **end** as a qualifier, never as the head noun.
  "Backend engineer … across regulated healthcare platforms" reads as an
  engineer who worked in healthcare. "Healthcare backend engineer" reads as
  domain-locked, and it is the single biggest lever on whether a non-domain
  company reads the resume as transferable.

### Sentence 2 — CAPABILITY

```
[delivery verb] + [4–6 technologies grouped as one coherent system story]
```

- This is the densest sentence and carries most of the technical weight.
- Group technologies by the system they form, not by category: "FastAPI and
  Spring Boot services with Kafka pipelines, PostgreSQL tuning, and Kubernetes
  deployment on AWS" is a delivery path. The same eight words as a comma list
  would read as inventory.
- Present tense, capability framing — "Delivers", "Designs", "Builds". Not
  "Responsible for".

### Sentence 3 — DIFFERENTIATOR

```
[the capability peers with this title usually lack] + [why it is credible]
```

- This is where a JD-relevant strength goes: GenAI, scale, domain depth, a
  second language, platform ownership.
- It must connect back to the identity rather than sit beside it. "Builds RAG
  and LLM capabilities into production workflows, **applying the same backend
  and observability discipline to GenAI systems**" — the trailing clause is
  what makes the AI work read as engineering rather than a side interest.
- Optional. Two strong sentences beat three with a weak third.

---

## Reference

The structure, annotated:

> **[S1 · IDENTITY]** Backend and platform engineer with nearly 4 years
> building distributed, event-driven systems in Python and Java across
> regulated healthcare platforms.
> **[S2 · CAPABILITY]** Delivers high-throughput FastAPI and Spring Boot
> services with Kafka pipelines, PostgreSQL tuning, and Kubernetes deployment
> on AWS, instrumented with OpenTelemetry and Datadog.
> **[S3 · DIFFERENTIATOR]** Builds RAG and LLM capabilities into production
> workflows, applying the same backend and observability discipline to GenAI
> systems.

11 technologies, 63 words, no objective sentence, no adjective opener, no JD
mirroring. Every clause is checkable against a bullet below it.

---

## Technology budget

| Slot | Count | Type |
|---|---|---|
| S1 | 1–2 | Languages only |
| S2 | 4–6 | Frameworks, data, infra — as a delivery path |
| S3 | 1–2 | The differentiating area |
| **Total** | **8–11** | — |

Selection rule, in priority order:

1. Terms in `must_retain` (base summary ∩ evidenced in bullets)
2. Terms in `keyword_budget` (required JD matches, capped at 6)
3. Terms carrying the most evidence weight in the settled bullets

A technology named in the summary **must appear in a bullet below**. No
exceptions — the summary is a claim, the bullets are the proof, and a reader
who checks and finds nothing discounts the whole document.

When the resume cannot evidence 8 technologies, the floor drops to the
allocated budget. The ceiling stays 11.

---

## What tailoring may and may not change

| Element | Tailorable |
|---|---|
| Which technologies fill S2 and S3 | **Yes** — this is the main lever |
| Ordering within a sentence | **Yes** — lead with JD-relevant terms |
| Which differentiator S3 carries | **Yes** |
| Domain qualifier emphasis | **Yes** — can soften for out-of-domain targets |
| Role family in S1 | **Only within the bridge budget** |
| Years, employers, metrics | **No** |
| Sentence count and structure | **No** |

The identity is the one thing that must not flex freely to the JD. A backend
engineer with ten backend bullets who calls themselves full-stack has written
a summary the rest of the resume contradicts, and the contradiction is
visible in ten seconds.

---

## Same base, three JDs

**Backend Engineer, fintech** — differentiator shifts to scale and
reliability; domain softened since healthcare is not an asset here:

> Backend engineer with nearly 4 years building distributed, event-driven
> systems in Python and Java. Delivers high-throughput FastAPI and Spring
> Boot services with Kafka pipelines sustaining 120K msgs/sec, PostgreSQL
> tuning, and Kubernetes deployment on AWS. Owns production reliability end
> to end, instrumented with OpenTelemetry and Datadog.

**Full-Stack Product Engineer** — bridge budget denies "full-stack engineer"
(backend bullets dominate), so identity holds and the bridge appears as
capability:

> Backend engineer with nearly 4 years building production APIs and data
> models in Python and Java across regulated healthcare platforms. Owns
> features end to end, from PostgreSQL schema design through Django and
> FastAPI services to React interfaces in the clinical UI. Ships to AWS with
> Kafka pipelines and production observability.

**Platform / Infrastructure Engineer** — identity tightens to platform only
when settled bullets support it; otherwise the candidate family holds and
infra leads S2:

> Platform engineer with nearly 4 years building distributed systems in
> Python and Java across regulated healthcare platforms. Owns containerized
> service deployment on EKS with Terraform-provisioned infrastructure and
> standardized Helm charts, backed by OpenTelemetry and Datadog
> instrumentation. Builds the backend services that run on it, from async
> FastAPI to Kafka streaming pipelines.

Same facts, same identity spine, different emphasis. None invents a claim,
none states an objective, none flips the candidate into a different kind of
engineer.

---

## Generation prompt rules

Positive requirements — every one must hold:

1. Exactly 2 or 3 sentences, 45–70 words.
2. S1 opens with the resolved `role_family` as the grammatical subject.
   Never an adjective, never the JD title verbatim.
3. Every sentence contains at least one technology, system, domain, or
   metric. A sentence with none is invalid — this is the rule that makes
   objective statements structurally impossible.
4. Every `must_retain` term appears.
5. 8–11 technologies total, per the slot budget (or the allocated floor
   when the resume is thinner).
6. Present tense, first-person-implied, no "I".
7. Domain qualifier trailing in S1, never the head noun.
8. Every sentence ends with a period.

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

Locked in `tests/test_summary_tailor.py`:

1. **Slot conformance.** Assert 2–3 sentences and that S1 contains a role
   family and ≤ 2 languages.
2. **Evidence per sentence.** Assert every sentence contains a term from
   `must_retain`, `keyword_budget`, or a numeric metric.
3. **Technology budget.** Assert 8–11 recognized technology terms total.
4. **Bullet backing.** Assert every technology named in the summary appears
   in at least one rendered bullet.
5. **Objective ban.** Each banned pattern → rejection.
6. **Domain position.** Assert the domain qualifier is not the first noun
   phrase in S1.
7. **Identity stability.** Same base resume, three different JDs → assert
   the role family changes only where the bridge budget permits it.
