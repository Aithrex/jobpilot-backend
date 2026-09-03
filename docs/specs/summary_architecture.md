# Summary architecture

Pipeline contract for the deferred professional-summary writer
(`services.summary_tailor`). Companion: [`summary_content_model.md`](summary_content_model.md)
defines **what the summary should say**. This document defines **how it is
produced**.

---

## Why it is deferred

The primary tailor writes bullets and skills only. The summary is a claim
about those bullets. Writing it first forces the model to invent an identity
and then retrofit evidence. Writing it last means the brief is built from
settled content, and a bad rewrite can fall back to the base resume summary.

There is no feature flag. This is the only summary writer.

## Phases

1. **Brief (deterministic).** `build_summary_brief` — no model call.
   Resolves `role_family` from the bridge budget (candidate family vs JD
   family vs settled-bullet support). Allocates `must_retain`,
   `keyword_budget`, and the S1/S2/S3 technology slots. Same inputs →
   identical brief.
2. **Generate (one PRO call).** `_build_generation_prompt` is the content
   contract. Retry once with the gate failures if the first draft fails.
3. **Accept comparatively.** Hard gates must pass. The candidate ships only
   when its score beats the base-resume summary. Otherwise the base ships.
   Grounding audit fail-closed: if `claim_guard` errors, keep the base.

## Bridge budget

| Support (share of settled bullets that match the JD family) | Identity |
|---|---|
| JD family == candidate family, or support ≥ 0.5 | Adopt the JD family label |
| 0.2 ≤ support < 0.5 | Keep the candidate family; add a bridge clause (capability, not title) |
| support < 0.2 | Keep the candidate family; no bridge |

The JD title is never copied verbatim. Seniority the candidate never held
is forbidden. `fullstack` is forbidden unless the budget adopts it.

## Fail-safe

On generation error, gate failure, weaker score, or grounding failure:
`tailored["summary"]` is the (cosmetic-cleaned) base summary. The writer
never raises.
