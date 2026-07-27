# Auto-Apply Incident Runbook (DoD item 12)

What to DO when auto-apply misbehaves: the kill switches, how to pause one user,
how to drain/DLQ the queue, how to work a `submission_unknown` backlog, and the
"stop everything" procedure. Every lever here is config-only (SSM in prod, `.env`
locally) unless noted — no code deploy required.

Cross-links: `docs/ops/auto-apply-observability.md` (the alarms that page you),
`docs/ops/auto-apply-rollout-rollback.md` (per-stage rollback), `docs/ops/gcp-executor-setup.md`
(queue/worker), `docs/ops/managed-mailbox-setup.md` (mail plane),
`docs/security/auto-apply-audit.md` (the controls).

---

## 0. Triage order

1. Is a USER being harmed (wrong/duplicate submits)? → **Stop everything** (§5).
2. Is ONE ATS failing? → per-adapter kill (§1.2).
3. Is ONE user affected? → pause that user (§2).
4. Is the queue backed up / retrying? → drain / DLQ (§3).
5. Ambiguous submits piling up? → `submission_unknown` backlog (§4).

---

## 1. Kill switches

### 1.1 Global (the master lever)
- `AUTO_SUBMIT_ENABLED=false` — the single most important switch. It is re-checked
  inside `auto_apply_service.presubmit_recheck` (the LAST gate before the click),
  so it stops **in-flight** runs BEFORE they submit, not just new ones. Setting it
  false also makes the whole `/api/apply/auto-submit/*` surface return 404.
- `EXECUTOR_REMOTE_ENABLED=false` — stops new invokes of the AWS Playwright
  `jobpilot-apply-executor` fleet. Rows stay `approved`/`ready` for later.
- `AUTO_APPLY_EXECUTOR=stub` — swaps the executor to the inert stub (no ATS I/O).

  > If an alternate GCP `cloud_run` plane is ever armed, also flip
  > `CLOUD_RUN_EXECUTOR_ENABLED=false`. Production Stage-4 uses AWS `remote`.

### 1.2 Per-adapter (dark one ATS family without a deploy)
- `AUTO_APPLY_ADAPTERS_DISABLED="greenhouse,workday"` (csv). Read by
  `ats_adapters.adapter_disabled()` and re-checked in `presubmit_recheck`, so a
  disabled family reports unsupported (`circuit_open`) and routes to a human. The
  in-process `CircuitBreaker` remains the AUTOMATIC drift-triggered layer; this is
  the MANUAL one. Read defensively — a config failure leaves adapters ENABLED,
  never darks the whole fleet by accident.

### 1.3 Per-domain (dark a single tenant/host)
- The ATS host allow-list is `AUTO_APPLY_ATS_ALLOWLIST` (csv, suffix-boundary),
  enforced by `ssrf_guard.apply_url_allowed` before dispatch AND by the worker's
  `context.route` interceptor. To dark a specific domain that was operator-added,
  REMOVE it from `AUTO_APPLY_ATS_ALLOWLIST`; its rows then fail the gate cleanly
  (`unsupported`) and never dispatch. For a whole family, prefer §1.2.
- Defense-in-depth: the Cloud Run egress firewall (devops-owned) is the
  infrastructure backstop even if a flag is missed.

---

## 2. Pause one user

- Preferred: set the user's autonomy mode to `copilot` (via
  `autonomy_service.set_mode(user_email, "copilot")` / the settings PUT) so the
  system stops auto-acting for them but they keep manual control.
- Harder stop: turn OFF their opt-in (`auto_submit_service.set_settings(user_email,
  False)`) — new approvals are refused (403 `auto_submit_opt_in_required`); their
  already-`approved` rows are still gated by the global `AUTO_SUBMIT_ENABLED` and
  `presubmit_recheck`.
- Their in-flight capability (if any) is one-use + short-TTL
  (`executor_capabilities`, 15 min) — it expires on its own; you do not need to
  revoke it manually, but you MAY delete the row to hard-stop a claim.

---

## 3. Drain / DLQ the queue

- **Pause dispatch (source):** `EXECUTOR_REMOTE_ENABLED=false` stops new AWS
  Playwright fleet invokes (and `CLOUD_RUN_EXECUTOR_ENABLED=false` if that
  alternate plane is ever armed).
  being enqueued. Existing tasks keep flowing to the worker unless you also pause
  the queue.
- **Pause the queue (sink):** pause the Cloud Tasks queue in GCP
  (`gcloud tasks queues pause <queue>`), so nothing is delivered to the worker.
  Resume with `... resume`.
- **DLQ:** the executor queue has bounded retry + a dead-letter policy
  (`infra/gcp/terraform/cloud_tasks.tf`; P5). A task that exhausts retries lands
  in the DLQ — alarm on DLQ depth > 0. A redelivered task is SAFE: the one-use
  capability claim (`claim_capability`, atomic CAS) makes a second dispatch a 403
  abort BEFORE any browser launches, and the Cloud Task name = `execution_id`
  de-dupes at the queue layer.
- **Never manually replay** a DLQ task for a row past the Submit boundary — see §4.

---

## 4. `submission_unknown` backlog

A `submission_unknown` is a timeout/lost-connection AFTER the Submit click — the
outcome is genuinely unknown, so it is **HELD and NEVER auto-retried** (control
C16; `finalize_submission` transitions to `STATUS_SUBMISSION_UNKNOWN` and stops).
Retrying risks a DUPLICATE application.

Work the backlog by hand:
1. Pull rows in `submission_unknown` (ledger, owner-scoped). Metric #4 is the
   count; correlate each to its `execution_id`.
2. For each, verify the true outcome out-of-band: the audit receipt (CMK-encrypted
   proof under `auto-apply-audit/<application_id>/<execution_id>/`), the ATS
   confirmation email in the managed mailbox, or the ATS portal.
3. Reconcile the ledger to the verified state:
   - Confirmed submitted → move to `submitted`/`confirmed`; the reservation
     settles (slot consumed).
   - Definitely NOT submitted → the safe pre-submit terminal path releases the
     reservation. Only then is a fresh attempt permissible.
4. Do NOT bulk-auto-resolve. The HOLD is the safety property; a human confirms.

---

## 5. "Stop everything"

In order, fastest blast-radius reduction first (all config-only):

1. `AUTO_SUBMIT_ENABLED=false` — stops the click everywhere, in-flight included.
2. `EXECUTOR_REMOTE_ENABLED=false` — stops new AWS Playwright fleet invokes.
3. `AUTO_APPLY_EXECUTOR=stub` — inert executor (no ATS I/O).
4. `APPLY_MAILBOX_ENABLED=false` — stops mail ingress/forward (halts inbound
   triggers + notifications).
5. `AUTO_APPLY_PREPARE=false` + `AUTO_APPLY_MODES=false` — stops the prepare stage
   so no new rows advance toward approval.

After the bleed stops:
- Confirm no row can submit: `presubmit_recheck` returns `ok=false`
  (`master_switch_off`) for every row.
- Work any `submission_unknown` backlog per §4.
- Capture the incident: which alarm fired, the `execution_id`/`adapter` in the
  metric lines (PII-free), and the audit trail (`auto_submit_audit`, append-only).
- Re-enable by walking the rollout stages backward-in (`auto-apply-rollout-rollback.md`)
  — do NOT flip everything back on at once.

---

## Invariants that make the above safe (do not weaken)

- One-use capability + callback token; per-resource one-use issuance
  (`executor_capability_service`).
- Atomic, status-guarded ledger transitions (`_atomic_transition`) + unique
  `(user_email, job_identity)` — a duplicate application is structurally
  impossible.
- The emailed approve magic-link GET is NON-mutating; the mutation is a JWT-authed,
  CSRF-safe POST consuming a single-use token (P1-APPROVE-GET).
- No secrets/PII in logs or metrics (control C6; enforced by `apply_metrics`).
- Business failures are 400/403/409/429 — NEVER 401.
