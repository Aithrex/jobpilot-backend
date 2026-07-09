# Learning / Growth engine — the résumé→hired loop

> Refined from the product owner's brief. Unifies Compass's **Learn** and **Signal**
> pillars into one closed loop, built as a bounded context (`services/learning/`,
> ADR-003). Serves a naïve user (guided, do-able) and an advanced user (ambitious,
> portfolio-grade) from the same engine. Goal: get the user **hired** and **noticed**.

## The loop
```
 résumé + live matched-job market
        │
        ▼
1. SUGGEST  →  2–4 in-demand, TRENDING gaps companies hire for that you're weak in,
   (§6.6)      that are SCARCE among your peers and BUILDABLE in ≤14 days.
        │      Rejects certs / courses / theory. Each shows why it's trending +
        │      why recruiters care + how many days to close.
        ▼
2. PICK ONE →  3. PLAN (7–14 days, Day N / Task M) — 100% PRACTICAL. Every task ends
        │       in a SHIPPABLE artifact, never "read the docs". Machine-checkable
        │       acceptance_criteria + a proof_type (github_repo|github_pr|url|writeup).
        ▼
4. ADAPTIVE DEPTH — same topic, two ceilings:
     • naïve  → GUIDED: starter scaffold, step hints, "we give you the skeleton"
     • adv.   → AMBITIOUS: open-ended constraints, harder extensions, solution withheld
     level inferred from résumé/GitHub; user can slide it.
        ▼
5. BUILD WITH OUR TOOLS — each task ships a starter scaffold + an AI build assistant
   grounded in the task, so the user implements INSIDE the product (basic→advanced).
        ▼
6. PUSH TO GITHUB — connect GitHub (existing connect/publish); create/sync the repo;
   concrete nudges ("commit this", "add a README explaining the tradeoff on X").
        ▼
7. MARK COMPLETE → VERIFY — GitHub existence + COMMIT-WINDOW + acceptance criteria
   (learning_verifier) → verified | needs_work (coaches, never punishes) | unverifiable.
        ▼
8. SIGNAL IT → LinkedIn post — on a VERIFIED task, auto-draft a specific, non-cringe
   post (reuses the post bandit + cringe guardrail), AUTO or MANUAL approval. This is
   how the user gets noticed.
        ▼
9. FEED BACK — the earned (proven) skill stages onto the profile → unlocks new
   matched jobs → the next SUGGEST. The loop compounds.
```

## What's built vs. remaining
- ✅ **Suggest** (`differentiation_service`, code-level reject filter, market-grounded
  `market_frequency`), **Plan** (7/14-day, acceptance_criteria + proof_type,
  theory-only tasks filtered), **Verify** (`learning_verifier`: GitHub existence +
  commit-window + criteria judge; SSRF-guarded url; only a verified task stages a
  skill). Routes: `POST /api/resume/learning/differentiators`,
  `POST /api/resume/learning/plan` (differentiator branch),
  `POST /api/resume/learning/paths/<id>/task/<ref>/verify`. Metered.
- ⏳ **Adaptive depth** (naïve/advanced level threading into plan generation — the
  route already accepts `level`), **build-assist** (per-task scaffold + grounded AI
  assistant), **GitHub push-sync** (create/sync repo + nudges, reusing the connect
  flow), **completion→verified→LinkedIn-post** (reuse post bandit + cringe filter,
  auto/manual approval), and the **frontend** (suggest cards → pick → plan → per-task
  build/verify → post). Packaged under `services/learning/` and made extractable per
  ADR-003.

## Integrity (non-negotiable)
- Never fabricate a pass: verification is grounded (existence + commit-window +
  criteria), and only a **verified** task earns a résumé skill.
- The completion post is grounded in the verified work; the cringe guardrail runs
  regardless of any engagement reward (it can never converge on bait).
- Untrusted GitHub/README content is fenced as data; no model output triggers a
  write — code decides.
