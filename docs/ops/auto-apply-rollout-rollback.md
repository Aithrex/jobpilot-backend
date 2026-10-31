# Auto-Apply Staged Rollout + Rollback (DoD item 12)

> **Retired 2026-10-31.** This describes the AWS setup, which has been deleted. It is kept for history. Current hosting: [`infra/compose/README.md`](../../infra/compose/README.md).

The exact feature-flag sequence to take auto-apply from internal fixtures to GA,
and the rollback for each stage. Every flag is a config value (SSM in prod via
`USE_SSM_SECRETS=true`, `.env` locally) read through `utils.config._get_config_value`
— flip it WITHOUT a code deploy. All flags are **dark by default**; a missing/false
value keeps the prior stage's behavior byte-identical.

Cross-links: `docs/ops/auto-apply-observability.md` (the alarms that GATE each
promotion), `docs/ops/auto-apply-incident-runbook.md` (kill switches / stop-
everything), `docs/ops/gcp-executor-setup.md` (worker + queue), `docs/ops/managed-mailbox-setup.md`
(mail plane), `docs/security/auto-apply-audit.md` (controls that must be green
before the execution plane carries real traffic).

---

## The flags (source of truth)

| Flag | Plane | Effect when ON |
|---|---|---|
| `APPLY_MAILBOX_ENABLED` | mail | managed-mailbox ingress/forward active |
| `AUTO_APPLY_PREPARE` | orchestrator | recon + answer resolution (prepare stage) runs; NO submit |
| `AUTO_APPLY_MODES` | orchestrator | the copilot/approval/autopilot mode machinery is live |
| `AUTO_APPLY_RESERVE` | entitlements | atomic reserve+settle for `auto_submit`/`autoapply_answer` (defect G) |
| `AUTO_APPLY_APPROVAL_FOLD` | approvals | canonical ledger rows surface in the extension pending feed (defect E2) |
| `AUTO_SUBMIT_ENABLED` | submit | the master submit kill switch — OFF makes the feature 404 + blocks the click |
| `AUTO_APPLY_EXECUTOR` | executor | selects the executor: `stub`\|`ats`\|`browser`\|`cloud_run` |
| `CLOUD_RUN_EXECUTOR_ENABLED` | executor | arms Cloud Task dispatch to the private worker |

Supporting: `AUTO_APPLY_ATS_ALLOWLIST` (operator-extensible ATS host allow-list),
`AUTO_APPLY_ADAPTERS_DISABLED` (per-family kill — see incident runbook),
`APPLY_MAIL_KMS_KEY_ID` / `APPLY_EXECUTOR_KMS_KEY_ID` (receipt/mail CMK pinning).

---

## Stages

Promote ONE stage at a time. After each flip: watch the observability dashboard
for the stage's gate window before advancing. Never skip a stage in prod.

### Stage 0 — Internal, fixture-only (dark)
- Flags: everything OFF. `AUTO_APPLY_EXECUTOR=stub`.
- What runs: the offline test suite + `stub`/`ats` fixtures only. No real ATS, no
  mail, no submit.
- Gate to advance: `docs/security/auto-apply-audit.md` P0 findings all closed
  (SSRF guard + egress FW); tests green.
- Rollback: n/a (nothing live).

### Stage 1 — Canary (prepare-only, no submit)
- Flip ON: `AUTO_APPLY_PREPARE=true`, `AUTO_APPLY_MODES=true` for the internal
  allow-list of canary users only. `AUTO_SUBMIT_ENABLED` stays **false**.
- What runs: recon + answer resolution + mode selection; rows reach
  `ready_for_review` and STOP. No click ever happens.
- Gate: task-start p95 (#2) and selector-drift (#6) healthy; no PII in metric
  lines (spot-check the log group).
- **Rollback:** set `AUTO_APPLY_PREPARE=false` (and/or `AUTO_APPLY_MODES=false`).
  In-flight rows halt at their current pre-submit state; nothing was submitted.

### Stage 2 — Approval-first beta (copilot/approval; human clicks)
- Flip ON: `AUTO_APPLY_APPROVAL_FOLD=true`, `AUTO_APPLY_RESERVE=true`. Keep
  `AUTO_SUBMIT_ENABLED=false`. Executor still non-dispatching.
- What runs: approved rows surface in the extension pending feed; the USER's own
  browser (extension) performs the submit. Quota is metered atomically (G). The
  emailed magic-link approve path is the NON-mutating GET + CSRF-safe POST
  (P1-APPROVE-GET) — verify the confirmation surface round-trips.
- Gate: success rate (#3) ≥ baseline; `submission_unknown` (#4) ~0; no double-
  charge (reservation counts match approvals).
- **Rollback:** `AUTO_APPLY_APPROVAL_FOLD=false` reverts the pending feed to the
  legacy `approved`-only bridge. `AUTO_APPLY_RESERVE=false` reverts to the legacy
  `check_and_increment` metering (byte-identical). No data migration needed.

### Stage 3 — Broader beta (wider cohort, still human-clicked)
- Same flags as Stage 2, expanded to the broader beta cohort. Enable
  `APPLY_MAILBOX_ENABLED=true` if the managed mailbox is part of the cohort
  (see managed-mailbox-setup).
- Gate: per-ATS success (#5) ≥ 0.50 for every active `adapter`; bounce/complaint
  (#8) within limits.
- **Rollback:** shrink the cohort allow-list; `APPLY_MAILBOX_ENABLED=false` stops
  mail ingress/forward.

### Stage 4 — Limited autopilot (headless submit, AWS remote fleet)
- Flip ON: `AUTO_SUBMIT_ENABLED=true`, `AUTO_APPLY_EXECUTOR=remote`,
  `EXECUTOR_REMOTE_ENABLED=true`. Limit to a SMALL user allow-list + a small
  set of ATS families (leave the rest in `AUTO_APPLY_ADAPTERS_DISABLED`).
- What runs: the AWS `jobpilot-apply-executor` Playwright Lambda fills the form,
  clicks submit (or freezes on CAPTCHA), uploads an SSE-KMS proof screenshot
  under `auto-apply-audit/`, and returns a terminal status. `submission_unknown`
  is HELD, never retried.
- Gate: `submission_unknown` (#4) ≤ threshold; success rate (#3) ≥ Stage 3;
  cost/attempt (#7) within budget; no sustained Errors/Throttles alarms.
- **Rollback (fast → full):**
  1. `AUTO_SUBMIT_ENABLED=false` — the master switch; re-checked inside
     `presubmit_recheck`, so it STOPS in-flight runs BEFORE the click.
  2. `EXECUTOR_REMOTE_ENABLED=false` — stops new Lambda fleet invokes (rows stay
     `approved`/`ready` for a later attempt).
  3. `AUTO_APPLY_EXECUTOR=ats` (or `stub`) — swaps the executor back.
  4. Per-family: add the offending ATS to `AUTO_APPLY_ADAPTERS_DISABLED`.

  > GCP `cloud_run` remains an alternate plane in code but is **not** the Stage-4
  > path; prefer AWS remote for scale-to-zero cost.

### Stage 5 — GA
- Widen the user allow-list to all eligible plans; empty
  `AUTO_APPLY_ADAPTERS_DISABLED` down to only genuinely-unsupported families.
- Gate: all Stage-4 alarms stable for a sustained window at volume.
- **Rollback:** identical to Stage 4's rollback ladder (the master switch is the
  one-flip revert).

---

## Rollback quick-reference

| Symptom | First flip |
|---|---|
| Any submit-safety doubt | `AUTO_SUBMIT_ENABLED=false` (stops the click, in-flight too) |
| Dispatch/fleet misbehaving | `EXECUTOR_REMOTE_ENABLED=false` |
| One ATS failing | add family to `AUTO_APPLY_ADAPTERS_DISABLED` |
| Quota mis-metering | `AUTO_APPLY_RESERVE=false` (revert to legacy metering) |
| Mail plane issue | `APPLY_MAILBOX_ENABLED=false` |
| Prepare-stage noise | `AUTO_APPLY_PREPARE=false` |

All flips are config-only (no deploy). The master switch `AUTO_SUBMIT_ENABLED` is
the single "stop clicking" lever; the rest narrow blast radius. See the incident
runbook for the full "stop everything" procedure.
