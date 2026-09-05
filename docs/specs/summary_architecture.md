# Summary architecture

Pipeline contract for the deferred professional-summary writer
(`services.summary_tailor`). Companion: [`summary_content_model.md`](summary_content_model.md)
defines **what the summary should say**. This document defines **how it is
produced**.

Writer revision: `WRITER_REVISION` in `summary_tailor.py`.

---

## Why it is deferred

The primary tailor writes bullets and skills only. The summary is a claim
about those bullets. Writing it first forces the model to invent an identity
and then retrofit evidence. Writing it last means the brief is built from
settled content, and a bad rewrite can fall back to the base resume summary.

There is no feature flag. This is the only summary writer.

## Phases

1. **Brief (deterministic).** `build_summary_brief` — no model call.
   Resolves the display label (broad SWE vs specialist), ranks evidenced
   terms and bullets for the JD, and selects optional domain / third-sentence
   capabilities. Same inputs → identical brief.
2. **Generate (one PRO call).** `_build_generation_prompt` is the content
   contract. Retry once with the gate failures if the first draft fails.
3. **Accept a valid draft.** Hard gates must pass. A valid candidate ships
   even when its informational score is below the base summary. Grounding
   audit fail-closed: if `claim_guard` errors, keep the base.
4. **Refresh after augmentation.** `refresh_summary_if_stale` recomputes the
   gap on the final resume. If named technologies are no longer backed,
   the writer runs once more. It never rewrites merely to make the text
   different.

## Role policy

| Situation | Display label |
|---|---|
| JD family is `swe` and the candidate has any supported engineering evidence | `Software engineer` |
| Specialist JD family matches the candidate, or support ≥ 0.5 | Adopt the specialist label |
| 0.2 ≤ support < 0.5 | Keep the candidate family; add a bridge clause |
| support < 0.2, or specialist unsupported | Keep the candidate family |
| Missing / `other` title | Responsibilities + a supported neutral label (`Software engineer` only with engineering evidence, else the candidate label or `Professional`) |

The JD title is never copied verbatim. Seniority the candidate never held
is forbidden. `fullstack`, frontend, ML research, and management labels are
forbidden unless the evidence budget adopts them.

## Sidecar

`tailored["summary_write"]` is a first-class sidecar (listed in
`SIDECAR_KEYS`). It never stores prompts or resume text.

| Field | Meaning |
|---|---|
| `writer_revision` | `WRITER_REVISION` |
| `role_label` | Display label used for generation |
| `source` | `generated` or `base` |
| `attempts` | 0–2 |
| `reasons` | Short codes (`shape`, `bullet_backing`, `grounding`, …) |
| `refreshed_after_augment` | True when the post-augment pass rewrote |

The tailor preview shows a non-blocking notice when `source` is `base`.

## Fail-safe

On generation error, gate failure, or grounding failure:
`tailored["summary"]` is the (cosmetic-cleaned) base summary. The writer
never raises.
