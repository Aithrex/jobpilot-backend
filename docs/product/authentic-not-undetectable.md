# Authentic, not undetectable

> Required reading, referenced from the prompt registry. Mandated by Compass
> addendum §6.12.1. This is a `DECISION LOCKED` stance: it does not get softened,
> A/B tested, or "temporarily" overridden.

## The stance

**No objective in this product — no scorer, no prompt, no reward, no eval — may
reward "sounds human" or "evades an AI detector" independently of "is true."**

The Voice Graph / Authored Resume feature optimizes exactly three terms and only
three:

1. **Grounding** — every claim traces to a `graph_evidence` span in something the
   user actually wrote or did. This is a *gate*, not a weight: a bullet that is not
   grounded scores zero and is discarded before anything else is computed.
2. **Specificity** — concreteness index: named entities, numbers, real artifacts.
3. **Voice distance** — `d(text, style_profile)`: how far the phrasing is from how
   this specific person actually writes.

## Why we refuse detector-evasion as a goal

In priority order (this ordering is itself part of the decision):

1. **Any objective that rewards "sounds human" without tying to "is true" is a
   hallucination pump.** It teaches the system that a fabricated, vivid detail
   outscores a real, plain one. That is the single worst failure this product can
   have, and it directly violates the never-fabricate guarantee already enforced by
   `integrity_guard.py` and `fabrication_filter.py`.

2. **AI-text detectors are unreliable and biased against non-native English
   writers** — which is precisely this product's core user (F-1 international
   students). Optimizing against a broken oracle would make our users' resumes
   *worse* to satisfy a tool recruiters barely use.

3. **The real defect is not "sounds like a model." It is "says nothing specific."**
   Specificity, grounding, and voice-match attack that defect directly and are
   measurable. "Undetectable" is neither the goal nor a proxy for the goal.

## What "human" means here

Not "evades a detector." **Authentic and traceable.** The output is factually the
user's (every claim maps to their own words or verified work) and sounds like them
(constrained to their measured voice). The `raw` provenance mode — zero
model-generated tokens, the user's own sentences reordered and tightened — exists
precisely so the honest floor is always available and always excellent.

## Enforcement (not just prose)

- `evals/resume_grounding/` — zero ungrounded bullets. Hard fail.
- `evals/entailment/` — Pass B ⊨ Pass A and Pass A ⊨ Pass B, claim by claim. Hard
  fail, and the same gate runs at **runtime**, not only in CI.
- `evals/anti_pattern_leak/` — zero banned words in authored output. Hard fail.
- There is deliberately **no** `evals/detector_evasion/` and there never will be.
  A reviewer who finds a "humanize / bypass-detector / undetectable" objective
  anywhere in this codebase should treat it as a P0 bug and delete it, citing this
  document.
