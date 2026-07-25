# Auto-Apply "Section 1" Architecture Defects — Execution Plan

Status: **verified against current code** (read-only investigation, both repos). Canonical backend
source is `shared/python/services/` (the `build/layer/python/…` copies are stale build artifacts —
ignore them). This is the build spec for defects A–L; implement in the dependency waves at the end.

## Verdicts

| Defect | Verdict | Repo |
|---|---|---|
| A Settings contract mismatch | REAL (BE now dual-reads; FE+extension pending) | both |
| B Answer gathering broken | REAL — every question silently dropped, coverage falsely 1.0 | backend |
| C Autonomy ignored | REAL — wrong signature, swallowed TypeError, fixed 0.90 threshold | backend |
| D No tailored resume | REAL — `tailoring_record_id` never populated on ledger rows | backend |
| E Two+ trackers conflict | REAL — 4 stores; mail writes `tailoring_records.application` | backend (+FE reads) |
| F Approval race | REAL — `approve_to_ready` accepts `queued`/`gathering` | backend |
| G Entitlements not atomic | REAL for `auto_submit`/`autoapply_answer`; tailor already fixed | backend |
| H Legacy AutoApplyTab double-approve | REAL — ledger `ready` rows shown as unapproved review cards | frontend |
| L saved_jobs uniqueness | REAL — unique on `job_id` alone | backend + index migration |
| I mail contracts | folds into E | backend |
| J stale deep links | REAL, minor — `/applications` not `/next` | both |
| K terraform owns executor flags | already correct | infra |

## A — Settings contract → canonical `auto_submit_enabled`
- FE `src/lib/api.ts` `updateAutoSubmitSettings` sends `{enabled}`; BE PUT reads `auto_submit_enabled`
  (400s otherwise); extension `extension/lib/autosubmit.js:~279` reads `s.data.enabled` (absent in GET
  → gate always off). FE hook + type already on `auto_submit_enabled`.
- Fix: FE send `{auto_submit_enabled}`, extension read `auto_submit_enabled`. **BE already dual-reads
  `auto_submit_enabled` then legacy `enabled`** (`services/jobs/blueprints/apply.py`).

## B — Answer gathering (typed field-id schema)
- `answer_library_service.resolve(user_email, questions: List[str])` skips non-strings and keys the
  result by **question text**; `auto_apply_service._call_answer_library` passes **dicts** → all dropped,
  `coverage:1.0`. Key mismatch: `_resolve_answers` looks up by `q["id"]`. Source allowlist
  `_DECLARED_SOURCES` is missing `declared` and `user_store` that the library emits.
- Fix: one normalized question shape `{field_id, label, type, required, is_legal_attestation, options?}`
  (stable `field_id`). `resolve` returns a **field_id-keyed** `resolved` map. `auto_apply_service`
  consumes `resolved[field_id]`; add `declared`+`user_store` to `_DECLARED_SOURCES`; keep the legal
  guard; required/sensitive-unanswered → `needs_input`, never guess. Land WITH C.

## C — Autonomy policy wiring
- `_decide_autonomy` calls `autonomy_service.score(**signals)`/`decide(score, **signals)` with wrong
  args → TypeError → swallowed → `_local_autonomy` with hardcoded `threshold=0.90`; per-user
  `get_autonomy_threshold` never used.
- Fix: call `autonomy_service.evaluate(user_email, app, answer_coverage=…, ats_family=…,
  has_novel_free_text=…, has_unconfirmed_legal_attestation=…, blocked_on_oracle=…)`. Remove the silent
  fallback as primary; keep it only for a true ImportError (WARN-logged), never for signature errors.

## D — Required tailored-resume preparation stage
- Queue/scheduler create paths never set `tailoring_record_id`; `_resume_ref` returns None → submit has
  no résumé.
- Fix: new async phase `prepare(application_id, user_email)` (branch in `resume_service._process_job`,
  mirroring `auto_apply_gather`/`auto_apply_submit`): select/create a tailored variant (reserve tailor
  quota via `quota_ledger_service`, idempotent — no re-tailor on redelivery), **pin an immutable
  version + rendered PDF in S3**, write `tailoring_record_id` back via `ledger.transition`, and only
  then allow `ready_for_review`. New canonical state `preparing`.

## E — ONE canonical `applications` state machine (HIGHEST RISK)
Four stores today, each with real readers/writers:
1. `applications` (ledger) — states `queued,gathering,needs_input,ready,submitting,submitted,confirmed,
   failed,skipped_duplicate,skipped`.
2. `tailoring_records.application` — parallel status; written by `mail_ingress_service._apply_tracker_status`,
   `gmail_service`, resume `PATCH /application`, `job_service.update_saved_job`; read by kanban,
   admin_metrics, pipeline_intel, today_inbox, digests.
3. `auto_submit_approvals` — approval lifecycle.
4. `saved_jobs.status`.

Target machine:
`draft→queued→preparing→needs_input→ready_for_review→approved→queued_for_execution→leased/running→
waiting_otp|waiting_human→submitted→confirmed`; terminal `cancelled, failed_pre_submit, unsupported,
withdrawn, submission_unknown`. Map legacy→canonical (`gathering→preparing`; split `ready` into
`ready_for_review`+`approved`+`queued_for_execution` — this enables F; `submitting→leased/running`;
`failed→failed_pre_submit`).

**Safe incremental path — NEVER big-bang:**
- E0: add new states + a `canonical_status()` normalizer, additive, no consumer change.
- E1: ledger becomes single writer of truth; add `set_tracker_status()` that writes the ledger AND
  mirrors to `tailoring_records.application`; migrate mail/gmail/PATCH writers to call it.
- E2: fold `auto_submit_approvals` into ledger `approved`/`queued_for_execution` (snapshot on the row).
- E3: flip every reader (kanban, dashboard, pipeline_intel, admin_metrics, digests, today_inbox,
  streaks, mail-correlation) to `canonical_status`; remove the mirror last. Backfill migration joins on
  `job_identity`/`source_job_id`.
- **Prerequisite: `qa-test-engineer` golden characterization tests on every reader/writer above.**

### E — two-axis clarification (discovered during E0; supersedes any "one status field" reading)

The four stores do NOT all track the same thing. The canonical `applications` row carries **two orthogonal axes**:

- **`status`** = EXECUTION lifecycle (did JobPilot get it submitted): the E0 machine
  `draft→queued→preparing→needs_input→ready_for_review→approved→queued_for_execution→running→
  waiting_otp|waiting_human→submitted→confirmed` + terminals. Owned by the auto-apply executor +
  approval flow. This is what the auto-apply queue / AutoApplyTab read.
- **`outcome`** = EMPLOYER-RESPONSE funnel (what happened after submission):
  `none→applied→viewed→screening→interviewing→offer→rejected→ghosted→withdrawn`. Owned by the
  mail/gmail/manual signals. This is what the Applications kanban board + `pipeline_intel_service.funnel`
  + `today_inbox` read (today via `tailoring_records.application.status`). **Your "rejection shows on the
  website" is this axis.**

So the migration is: `applications.status` already exists (execution). E1 ADDS `applications.outcome`
(+ `outcome_source`, `outcome_evidence`, `outcome_updated_at`) and makes the mail/gmail/PATCH writers set
`outcome` on a find-or-created canonical row, while STILL mirroring the coarse value to
`tailoring_records.application.status` so the kanban/funnel golden readers stay byte-identical until E3
flips them to read `applications.outcome` (via a small `outcome_view` that maps to the funnel's current
buckets). A manually-tracked or mail-only application (no auto-apply row) gets a lightweight canonical
row with `origin ∈ {manual, mail, gmail}` and `status="external"` (a new EXECUTION-axis terminal meaning
"submitted outside our executor") so it never enters `ACTIVE_STATUSES`/`dedup_gate`.

E1 order (one writer at a time, each guarded by the golden readers):
1. `ledger.set_outcome(user_email, job_ref|record_id, outcome, source, evidence)` — find-or-create the
   canonical row (link `tailoring_record_id`), set the outcome axis, mirror to `tailoring_records`.
2. Migrate the **mail** writer (`mail_ingress_service._apply_tracker_status`) first (newest, lowest-traffic,
   already golden-covered), prove kanban/today_inbox goldens unchanged.
3. Then **gmail**, then the **PATCH /application** route — the route needs its own characterization test
   first (streak `record_application` on draft→applied + `saved_jobs` sync are side effects the golden net
   does not yet cover).

### E — E2/E3 status (DONE, dark + dual-read; mirror KEPT)

- **E2 (fold approvals) — DONE, dark.** `auto_submit_service` is now a thin adapter over the ledger's
  F states: `list_canonical_approvals()` surfaces `ready_for_review` (`already_approved:false`) +
  `approved`/`queued_for_execution` (`already_approved:true`, read-only for defect H) LABELED with the
  canonical `status`; `approve_ledger()` routes new approvals through `ledger.approve()` (F snapshot).
  Gated by `AUTO_APPLY_APPROVAL_FOLD` (default **OFF everywhere**); `/auto-submit/pending` keeps the
  legacy `approved`-only bridge until the flag is on. Legacy `auto_submit_approvals` docs are STILL read
  and are **not deleted**.
- **E3 (flip readers) — DONE, canonical-preferred DUAL-READ.** `application_ledger_service.outcome_view(row)`
  collapses the two axes (`outcome` + `canonical_status(status)`) onto the coarse funnel bucket, and
  `outcome_buckets_for_records(user_email)` is the per-record preference map. `pipeline_intel_service.funnel`
  and (resume-local) `job_intelligence_service.get_funnel_analytics` PREFER the canonical row when one
  exists, else FALL BACK to `tailoring_records.application.status`. `today_inbox_service`'s status surface
  (`needs_input`) already reads the canonical ledger; `admin_metrics_service` has no
  `application.status` funnel aggregation, so there is nothing to flip there.
- **Backfill:** `scripts/backfill_applications_canonical.py` projects legacy
  `tailoring_records.application.status` + live `auto_submit_approvals` onto canonical rows (find-or-create
  via `set_outcome` + the approval fold); idempotent + no-op on a clean DB.
- **Race guard index:** unique PARTIAL `(user_email, tailoring_record_id)` added to
  `scripts/migrate_indexes.py` + `application_ledger_service.ensure_indexes()`.
- **DEFERRED cleanup — DROP THE MIRROR.** The writers STILL dual-write `tailoring_records.application`
  and the readers KEEP the fallback, so the char goldens stay byte-identical. Removing the mirror (writers
  stop dual-writing; readers drop the tailoring_records fallback and read the canonical row directly) is a
  LATER, separate change — do it only once every reader is canonical-first in prod and a backfill has run.

## F — Approval only from `ready_for_review` + frozen snapshot
- `approve_to_ready` accepts `queued/gathering/needs_input`; no snapshot frozen.
- Fix: `_APPROVABLE_STATUSES = (ready_for_review,)`; approve `ready_for_review→approved`. Freeze
  `application_hash, resume_version_id, answers_fingerprint, policy_version, consent_version`. Re-validate
  at execution; stale snapshot → back to `ready_for_review`/`needs_input`, never submit stale. Approval
  emails only fire once a row reaches `ready_for_review`.

## G — Atomic exactly-once entitlement reservation
- `entitlements_service.check_and_increment` and `usage_service.check_and_increment` are check-then-
  increment (racy) for `auto_submit`/`autoapply_answer`. Correct pattern EXISTS: `quota_ledger_service`
  atomic guarded reserve (`_atomic_reserve`, `reserve_tailor`, `complete_entry`, `refund_entry`); tailor
  already migrated.
- Fix: generalize `quota_ledger_service` to a `feature` param; add `reserve(email, feature, n,
  idempotency_key)`. Wire lifecycle: **reserve** before dispatch/at approval; **finalize** on
  `confirmed`; **release** on `failed_pre_submit/unsupported/cancelled/withdrawn`; **hold** (stay
  reserved) on `submission_unknown`. `idempotency_key = application_id + snapshot` so redelivery never
  double-reserves. Keep the daily-cap numbers unchanged.

## H — Legacy AutoApplyTab double-approval
- `/auto-submit/pending` merges already-approved ledger `ready` rows as `status:"approved"`; FE renders
  ALL pending as actionable ReviewCards → re-approve → `apply.py` charges `auto_submit` again + new
  approval. Also `key={approval_id}` is undefined for ledger rows.
- Fix (after E splits states): BE labels rows (`already_approved`); FE renders `approved`/
  `queued_for_execution` as read-only "Approved & submitting" cards, only `ready_for_review` as
  actionable; approve hits the idempotent ledger path (no re-charge — belt-and-suspenders with G).

## L — saved_jobs owner-scoped uniqueness
- Global unique on `job_id` alone (`job_service.py:~309`, `migrate_indexes.py:~154`) blocks a second
  user saving the same posting. `job_id` is already source-prefixed (per-posting canonical).
- Fix: compound unique `(user_email, job_id)`; safe reindex = create compound, verify, drop `job_id_1`
  (cannot fail on existing data since today ≤1 row per job_id).

## I / J / K
- I: the mail classifier is sound (closed schema, grounded evidence, confidence gate); its only issue is
  writing to `tailoring_records.application` instead of the ledger → folded into E1.
- J: email approve/review links point at `{FRONTEND_URL}/applications`; update to the `/next` route.
- K: executor flags (`AUTO_APPLY_EXECUTOR`, `AUTO_SUBMIT_ENABLED`) already read via config/SSM and owned
  by `infra/terraform/lambda.tf`+`schedulers.tf`. No in-repo change; flips are an operator action.

## Autonomy MODES (explicit Copilot / Approval-first / Autopilot) — layered on F

Today there is only an implicit per-user `autonomy_threshold` on `auto_submit_settings` +
`autonomy_service.decide() -> auto|ask|hold`. There is NO explicit mode and NO autopilot graduation
gate. Add an explicit `mode` as the SINGLE source of truth on `auto_submit_settings.mode`
(the managed_mailboxes.mode field becomes a read-through of it — one source, no drift):

DECIDED (user, July 2026): **no implicit default — onboarding step 4 FORCES an explicit pick** among
the three; Autopilot graduation gate `N = 10`; Copilot is **prepare-only (no automation)**.

- **`copilot`** (Manual, prepare-only): JobPilot prepares the tailored résumé + resolves answers and
  presents them (an "answer sheet"); **NO browser automation, NO executor dispatch, NO submit** — the
  user opens the job and applies themselves. The row lands in a `ready_for_review`-style "ready to apply
  manually" disposition and never enters the execution path. `decide()` is irrelevant (never auto).
- **`approval`** (Approval-first): cloud prepares; **every** application waits for explicit user approval
  regardless of confidence. `decide()` is capped — `auto` is downgraded to `ask`.
- **`autopilot`** (full auto, no per-application permission): `decide()` may return `auto` (confident +
  all gates pass); when NOT confident or a sensitive/legal/novel field is present, it falls back to
  `ask`/`hold` (never fabricates). Requires (a) explicit opt-in AND (b) a **graduation gate**: at least
  **10** successfully reviewed+confirmed applications before `auto` is honored; until then `auto -> ask`
  (count from the canonical `applications`: rows that reached `confirmed` via a user-approved path).

Storage: `auto_submit_settings.mode ∈ {copilot, approval, autopilot}` is the single source of truth;
`managed_mailboxes.mode` reads through to it. No default is written — a mailbox/settings read returns
`mode: null` until onboarding sets it, and the executor/scheduler refuse to act while `mode` is unset.
Gate location: apply the mode AFTER `autonomy_service.decide()` computes the raw decision, in the
gather/approve path (post-F). Hard never-fabricate rules (unconfirmed legal attestation -> ask) win in
ALL modes, including autopilot.

## Dependency-ordered execution
- **Wave 0 (independent, now):** A (FE+extension), L (index), + **characterization golden tests** (blocking prereq for Waves 2–3).
- **Wave 1:** B + C together (gather correctness).
- **Wave 2 (serialized, gated on Wave-0 tests):** E0/E1 (additive states + shim + migrate writers), then D (prepare stage).
- **Wave 3 (after E0/D):** F (approval snapshot) + G (reservation), parallelizable.
- **Wave 4:** E2/E3 (fold approvals, flip readers, backfill) + H (FE) + J.
- **Highest risk:** E — mitigate with the additive `canonical_status()` shim + write-through mirror +
  reader-by-reader cutover, only after golden tests exist.
