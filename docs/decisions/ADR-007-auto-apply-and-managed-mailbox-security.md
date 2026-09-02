# ADR — Auto-Apply + Managed-Mailbox Security Architecture

Status: **Accepted (implementation in progress, behind dark flags).** Scope: the
two planes of the auto-apply feature — the **mail-ingress plane** (recruiter mail
arrives at a managed address) and the **auto-apply execution plane** (a remote
headless browser fills + submits an ATS form). This ADR records the load-bearing
security decisions and ties each to the code that implements it. It is a
companion to `docs/specs/managed-mailbox-v1.md`, `auto-apply-defects-plan.md`,
and `auto-apply-fill-engine.md`.

All file paths are relative to `jobpilot-backend/` unless noted.

---

## ADR-1 — Inbound mail via Cloudflare Email Routing, NOT SES

**Decision.** Inbound recruiter mail for the neutral `APPLY_MAIL_DOMAIN` is
received by a Cloudflare Email Routing catch-all → a thin Email Worker → an
HMAC-signed webhook to the backend. SES/SNS is explicitly abandoned for inbound
(the legacy `mail_ingress_service` SES path was never wired).

**Rationale.** No per-user mailbox provisioning, real-time delivery, free at our
volume, and the Worker can be kept credential-light (it holds only the HMAC
secret + an ephemeral presigned URL). The Worker does **no** MIME parsing and
holds **no** AWS credentials, shrinking the attacker surface at the edge.

**Implements.**
- `infra/cloudflare/email-worker/src/worker.js` — thin Worker: size gate,
  HMAC sign, two-phase handoff, no parsing, no AWS creds.
- `services/resume/blueprints/mail_inbound.py` — JWT-exempt, HMAC-authed
  `/api/resume/mail-inbound/cloudflare/{slot,commit}`.
- `shared/python/utils/webhook_hmac.py` — constant-time `hmac.compare_digest`
  verify; empty secret fails closed.

**Consequence / residual.** The HMAC secret becomes the single edge trust anchor
(see threat model T1/T2). SPF/DKIM/DMARC are captured as **evidence only**, not
enforced as an accept/reject gate (`mail_store_service._parse_auth_results`).

---

## ADR-2 — Two-phase presigned-MIME ingest (raw bytes never transit API Gateway)

**Decision.** Inbound is a two-phase protocol: **slot** (`POST /slot`) resolves
the owner and mints a one-use, single-object, short-TTL presigned **S3 PUT** plus
an opaque `commit_token`; the Worker PUTs the raw MIME **straight to S3**; then
**commit** (`POST /commit`) claims the token, verifies the uploaded object's
sha256, parses, and stores.

**Rationale.** Raw MIME can reach 25 MiB, over the API Gateway 10 MB body cap.
Routing bytes directly to S3 also keeps untrusted MIME out of the Lambda request
path until it has been size-gated and hash-verified. The commit token binds the
presigned capability to a resolved owner + message id so the Worker can never
redirect an upload to another user's object.

**Implements.**
- `mail_inbound.py::cloudflare_slot` → `store.mint_commit_token` +
  `get_storage_service().generate_presigned_put(raw_s3_key, …)`.
- `mail_store_service.raw_key_for` — deterministic, owner-hashed key
  `mail/raw/{sha256(email)}/{safe(message_id)}.eml` (both sides agree without
  threading the key through the Worker).
- `mail_inbound.py::cloudflare_commit` → `store.claim_commit_token` (atomic
  one-use CAS) → sha256 re-verify → `store.ingest_committed`.
- `worker.js` — PUTs to `upload_url` with the returned headers; its **only**
  capability is that one-use URL.

**Consequence / residual.** The slot/commit canonical strings carry no
timestamp/nonce, so a captured signed request is replayable; the
`(alias_id, provider_message_id)` unique index and one-use token make replay
idempotent rather than harmful (threat model T-REPLAY).

---

## ADR-3 — Canonical two-axis `applications` state machine

**Decision.** One canonical `applications` ledger row per `(user_email,
job_identity)` carries **two orthogonal axes**: `status` (execution lifecycle —
did JobPilot get it submitted) and `outcome` (employer-response funnel — what
happened after). Every transition is an atomic, status-guarded compare-and-set;
the unique `(user_email, job_identity)` index makes a duplicate application
structurally impossible.

**Rationale.** Four competing stores (`applications`, `tailoring_records.
application`, `auto_submit_approvals`, `saved_jobs.status`) were the highest-risk
defect (E). A single canonical writer with a CAS guard is the only way to
guarantee two racing Lambdas can never both submit, both fail, or both requeue —
the correctness spine that also underpins the anti-duplicate-submission control.

**Implements.**
- `shared/python/services/application_ledger_service.py` —
  `normalized_job_identity`, `dedup_gate`, `_atomic_transition` (CAS),
  `_TRANSITION_GUARDS`, `canonical_status`/`outcome_view` read-shims, unique
  indexes in `ensure_indexes`.
- Owner scoping: **every** filter folds in `user_email`; `job_identity` is
  validated to a 64-hex sha256 (`_require_identity`) before it can reach a filter.

**Consequence / residual.** The migration is deliberately additive
(E0→E1→E2→E3) and gated on golden characterization tests; until E3, the coarse
`tailoring_records.application.status` mirror is still written and read.

---

## ADR-4 — Atomic, exactly-once entitlement reservation

**Decision.** Auto-submit entitlement is **reserved** atomically at approval
(guarded `$inc` bounded below the plan cap, idempotency-keyed on
`application_id + snapshot fingerprint`), then **settled** at the terminal
execution state: finalize on `confirmed`, release on safe pre-submit terminals
(`failed`/`unsupported`/`cancelled`/`withdrawn`), **hold** on
`submission_unknown`.

**Rationale.** The prior `check_and_increment` was check-then-increment (racy),
double-charging under concurrent Lambdas / replayed magic links. The atomic
reserve/settle pattern (proven for tailor quota) closes the race and makes a
redelivery reuse the same reservation.

**Implements.**
- `shared/python/services/quota_ledger_service.py` — `reserve` (feature-param),
  `_atomic_reserve` (guarded `$inc`), `complete_entry`/`refund_entry`/
  `refund_entries` (idempotent via the reserved→settled state guard).
- `application_ledger_service.approve` (G HOOK — reserve before the CAS),
  `_settle_reservation` (finalize/release/hold), `_auto_submit_idempotency_key`.
- Dark-flag `AUTO_APPLY_RESERVE` (default OFF) so existing flows stay
  byte-identical until flipped.

**Consequence / residual.** Correct settlement depends on every terminal
transition flowing through `ledger.transition` (it does — the executor terminal
callback finalizes via `finalize_submission` → `mark_*`).

---

## ADR-5 — GCP Cloud Run isolation via Workload Identity Federation, no static keys

**Decision.** The browser worker runs as a **private** Cloud Run service
(`INGRESS_TRAFFIC_INTERNAL_ONLY`, no `allUsers`, concurrency 1, scale-to-zero).
The AWS resume Lambda enqueues Cloud Tasks by **federating** its IAM role into a
least-privilege GCP enqueuer SA via WIF — **no static service-account key** ever
exists AWS-side. The task carries the invoker SA's OIDC token; Cloud Run enforces
the invoker identity at the platform edge.

**Rationale.** A static GCP key stored AWS-side would be a long-lived,
exfiltratable secret. WIF removes it entirely and pins acceptance to the exact
AWS account + exact assumed-role. The worker runtime SA holds **nothing** durable
(no Mongo, no SSM, no bucket-wide S3, only log-write) — everything it needs is
handed to it one-use and short-TTL.

**Implements.**
- `infra/gcp/terraform/wif.tf` — pool + provider with an attribute-condition
  pinning `assertion.account` and `assumed-role/<role>/`; enqueuer SA with
  `workloadIdentityUser` + `actAs` only.
- `infra/gcp/terraform/cloud_run.tf` — internal-only ingress, invoker-SA-only
  `run.invoker`, concurrency 1.
- `infra/gcp/terraform/service_accounts.tf` — runtime SA = `logging.logWriter`
  only; invoker SA = invoke that one service only.
- `shared/python/services/cloud_run_executor.py` — `submit()` mints a capability
  + enqueues ONE task carrying only `{execution_id, capability}` (no answers,
  résumé, creds, PII).

**Consequence / residual.** The worker still drives a full browser to an
attacker-influenceable `apply_url`; Cloud Run VPC egress restriction + metadata
blocking are **not yet** in the `.tf` (audit finding P0-1/P0-2). The runtime SA's
blast radius is small, but the GCP metadata server remains reachable from the
container absent an egress control.

---

## ADR-6 — One-use capability + callback-token security seam

**Decision.** The worker's only starting secret is a one-use `capability` in the
task body. Its first act is an atomic **claim** (CAS) that leases the capability
exactly once and returns a scoped `callback_token`; every follow-up
(resume-url / credential / receipt-url / precheck / milestone / terminal) is
authenticated by that token. Each *resource* issuance is independently one-use
(per-execution flags). Only token **digests** are persisted.

**Rationale.** A redelivered Cloud Task racing a second claim loses (→403) —
the double-dispatch backstop **before any browser launches**. A leaked callback
token cannot re-pull the résumé or re-reveal a credential (one-use flags). A DB
read can never recover a live token (SHA-256 digests only). Machine-route auth
failures are **403, never 401** (invariant #1 — a machine caller has no session
to expire).

**Implements.**
- `shared/python/services/executor_capability_service.py` — `mint_capability`,
  `claim_capability` (atomic CAS), `verify_callback`, `_mark_issued` one-use
  flags, `_digest`, TTL index.
- `services/resume/blueprints/executor.py` — the callback surface; all failures
  `_forbidden()` = 403; owner identity read from the bound token, never the body.
- `issue_resume_get` / `issue_receipt_put` — presigned URLs scoped to the ONE
  pinned résumé key / the ONE audit-prefix receipt key; never bucket-wide.
- `issue_credential` → `ats_credential_service.reveal` (KMS-envelope vault),
  one-use, plaintext never logged.

**Consequence / residual.** A callback token leaked within its 15-min TTL can
still make **one** of each un-consumed call; the presubmit re-check and one-use
flags bound the damage. The `/credential` response returns plaintext over TLS to
`aspirely.me` (threat model T-CRED).

---

## ADR-7 — Never-fabricate answer gating (deterministic fill; model classifies, code fills)

**Decision.** The fill engine never invents an answer. A canonical field catalog
+ deterministic normalizer maps recon fields to stable `field_id`s (with negation
detection); a resolver pulls the user's onboarding-declared value; an option
matcher maps it to the form's actual option. Any `protected`/`legal` field with
no explicit user-declared value, any required-missing field, any low-confidence
mapping, CAPTCHA/MFA/account-gate, or novel legal attestation → `needs_input` /
`waiting_human` — never a guess. The LLM only *classifies* field text into the
**closed** catalog; it never supplies a value.

**Rationale.** Fabricating work-auth / EEO / veteran / disability / salary-history
/ criminal-history / legal attestations is both an integrity violation
(IntegrityGuard) and a legal/discrimination risk. A model in the fill path is a
prompt-injection sink (recruiter form text is untrusted).

**Implements.**
- `shared/python/services/application_question_bank.py` — catalog with
  `SENS_PROTECTED`/`SENS_LEGAL`, `never_default`/`decline_default`;
  `_GATED_SENSITIVITIES`; `normalize_field(allow_llm=…)`.
- `shared/python/services/apply_forms.py::normalize_recon_questions` —
  deterministic-only in the gather loop (`allow_llm=False`); `is_legal_attestation`
  is OR-merged (recon ∨ bank ∨ classifier) so a legal field can never be
  down-graded.
- `ats_adapters.py` — `_novel_legal_attestation`, `_check_takeover`
  (captcha/mfa/account/legal), `submit()` pauses to `waiting_human`; LinkedIn
  `NEVER_AUTOMATE`, Workday unsupported stub, Generic never auto-submits.

**Consequence / residual.** Correctness depends on the onboarding declared store
being the sole answer source; the executor fills only `field_id→value` from the
integrity-guarded `execution_envelope.answers`.

---

## ADR-8 — Dark-flag rollout everywhere

**Decision.** Every live surface is gated OFF by default and flipped only after
external infra + staging smoke pass: `APPLY_MAILBOX_ENABLED`,
`CLOUD_RUN_EXECUTOR_ENABLED`, `AUTO_APPLY_RESERVE`, `AUTO_APPLY_EXECUTOR`
selection, `AUTO_SUBMIT_ENABLED` master kill-switch (re-checked at
`presubmit_recheck`), plus `APPLY_MAIL_DOMAIN` empty → feature inert.

**Rationale.** Both planes touch real employers and real user PII; a
misconfiguration must fail *closed* to today's behavior, never open. The master
kill-switch is re-evaluated **inside** the pre-submit gate so flipping it OFF
stops in-flight runs before the irreversible click.

**Implements.**
- `managed_mailbox_service.mailbox_enabled` / `apply_mail_domain`.
- `cloud_run_executor._enabled` / `is_available`.
- `application_ledger_service._reserve_enabled`.
- `auto_apply_service.presubmit_recheck` — `kill_switch` check
  (`_auto_submit_enabled()`), plus pause/revocation, approval freshness/TTL,
  snapshot drift, entitlement-held, adapter health, posting availability.
- Executor flags owned by external `infra/terraform/lambda.tf` +
  `schedulers.tf` (flips are an operator action).

**Consequence / residual.** A **per-adapter** operator kill-switch is not yet a
config knob (only the in-process, cold-start-resettable `CircuitBreaker` +
the global flag) — see audit finding P1.
