# Security Controls Audit — Auto-Apply + Managed Mailbox (§9 mega-spec)

Read-only audit of the *implemented* code against the mega-spec §9 mandatory
controls. Verdict per control: **IMPLEMENTED** (cite code) / **PARTIAL** /
**MISSING**. Every PARTIAL/MISSING has a remediation in the prioritized findings
list at the end — that list is the input to the controls-implementation task.

Paths are relative to `jobpilot-backend/`. This audit changed **no** code.

---

## Control-by-control

### C1 — HTTPS host allow-list with suffix-boundary matching
**MISSING (execution plane) / PARTIAL (detection only).**
`ats_adapters` detects families by host needle (`_host` + `_HOST_NEEDLES`, e.g.
`greenhouse.io`, `lever.co`) but that is *detection*, not an *allow-list gate*.
The worker navigates whatever `apply_url` the envelope carries —
`worker.py::_run` → `adapter.open(page, apply_url)` — and `apply_url` comes
straight from the ledger row (`auto_apply_service.execution_envelope` →
`app.get("apply_url")`), which originates from a job posting (attacker-influence).
No `https`-only check, no suffix-boundary host allow-list, before navigation.
→ **Finding P0-1.**

### C2 — Block private / loopback / link-local / metadata / reserved IPv4 + IPv6
**MISSING.** No IP/range validation anywhere in the execution path
(`worker.py`, `ats_adapters`, `execution_envelope`). The browser can resolve and
connect to RFC1918 / `127.0.0.0/8` / `169.254.0.0/16` / `::1` / `fc00::/7` /
`169.254.169.254` targets. → **Finding P0-1 / P0-2.**

### C3 — Revalidate redirects / popups / downloads / websockets / subresources
**MISSING.** `worker.py` launches chromium with no route interception; adapters
call `page` methods directly. A redirect from an allow-listed host to an internal
host, a popup, a background subresource, or a websocket is not revalidated.
→ **Finding P0-1.**

### C4 — Playwright request interception + network egress restriction
**MISSING.** No `page.route(...)` / `context.route(...)` request interceptor in
`worker.py`, and no network-egress restriction on the Cloud Run service —
`infra/gcp/terraform/cloud_run.tf` sets ingress internal-only but places the
service on default egress (no VPC connector / no egress firewall). → **Finding
P0-2 (infra) + P0-1 (interceptor).**

### C5 — Deny file: / data: / custom schemes / localhost / RFC1918 / metadata
**PARTIAL.** In *mail* HTML sanitization these are handled
(`mail_store_service._DANGEROUS_URL` blocks `javascript:`/`data:`/`vbscript:`,
remote images off). In the *browser* navigation path there is **no** scheme
deny-list. → **Finding P0-1** (execution plane).

### C6 — No secrets / PII in logs
**IMPLEMENTED.** Consistent and careful:
- `worker.js` logs only sizes / message-id / status codes (documented rule).
- `worker.py` logs execution/application ids + coarse status; credentials held in
  memory, `creds = None` after use, never logged.
- `mail_forward_service` never logs `forward_to`/subject/body — only booleans +
  `thread_id` (`_skip`); verification never logs the address.
- `executor_capability_service` stores only token **digests** (`_digest`); the
  `/credential` route returns plaintext but never logs it.
- Ledger `_audit` meta carries counts/prefixes only (`job_identity[:12]`).
Residual: confirm the resume Lambda's request logging / X-Ray does not capture
`/executor/credential` response bodies (X-Ray is on when `USE_SSM_SECRETS`).
→ minor verification, **Finding P2-LOG.**

### C7 — Least-privilege IAM
**IMPLEMENTED (GCP).** Runtime SA = `logging.logWriter` only
(`service_accounts.tf`); invoker SA invokes one service only (`cloud_run.tf`);
enqueuer SA = `workloadIdentityUser` + `actAs` + `cloudtasks.enqueuer` on one
queue (`wif.tf`, `cloud_tasks.tf`); WIF attribute-condition pins account + exact
role. No static SA key. AWS-side S3/KMS grants for the callback surface are
scoped to the pinned résumé key + audit prefix by construction
(`executor_capability_service.issue_*`). Verify the resume Lambda's IAM policy
(external `infra/terraform/`) restricts S3 to `auto-apply-audit/*` + résumé
prefix and KMS to the vault CMK. → **Finding P2-IAM** (verify AWS side).

### C8 — Encryption at rest / in transit
**PARTIAL.**
- Mail bucket: SSE-KMS enforced — `ingest_committed` passes `kms_key_id` on every
  `put_object_bytes`; presigned PUT for raw passes `kms_key_id=store._kms_key_id()`.
- In transit: all callbacks are HTTPS to `aspirely.me`; Cloud Tasks→Cloud Run is
  Google-internal + OIDC.
- **Gap:** the executor **receipt** PUT (`issue_receipt_put`) calls
  `generate_presigned_put(key, content_type="image/png")` with **no** `kms_key_id`
  and no explicit bucket, and `issue_resume_get` presigns with no bucket — both
  rely on the default bucket's default encryption. The audit receipt contains a
  full-page screenshot of a filled form (PII). → **Finding P1-ENC** (pin the
  audit prefix to a CMK; confirm default-bucket SSE).

### C9 — Append-only audit
**PARTIAL.** `application_ledger_service._audit` and
`executor.py::milestone` → `ledger._audit` only ever `insert_one` into
`auto_submit_audit` (append-only by convention). No DB-level immutability / WORM
and no IAM denial of update/delete on that collection. → **Finding P2-AUDIT.**

### C10 — Owner-scoped authorization
**IMPLEMENTED.** Every mail + ledger + capability read/write folds in
`user_email` and returns 404 (not 403) on a foreign id; ids are
`InputSanitizer`-sanitized before any filter. User routes use
`get_jwt_identity()`; machine routes read identity from the bound token. Résumé /
receipt keys are server-derived from the owner-bound row/token
(`pinned_resume_s3_key`, `issue_receipt_put`). Verified across
`mail_store_service`, `managed_mailbox_service`, `application_ledger_service`,
`executor_capability_service`. No IDOR found.

### C11 — One-use caps (capabilities, tokens, presigned URLs)
**IMPLEMENTED.** Commit token one-use CAS (`claim_commit_token`); executor
capability one-use CAS (`claim_capability`); per-resource one-use flags
(`_mark_issued` for resume/credential/receipt); terminal one-use
(`mark_terminal_reported`); forward-verification token one-use
(`confirm_forward_verification`). All presigned URLs are short-TTL (300–600 s
execution; 300 s mail read).

### C12 — Atomic leases / idempotency
**IMPLEMENTED.** Ledger transitions are status-guarded CAS (`_atomic_transition`);
unique `(user_email, job_identity)` backstop; Cloud Task name = `execution_id`
de-dupes the queue; `(alias_id, provider_message_id)` unique index for mail
idempotency; quota reservation idempotency-keyed. `submission_unknown` held,
never auto-retried (`finalize_submission`).

### C13 — Rate limits (global / per-user / per-domain / per-ATS)
**PARTIAL.**
- Global: Cloud Tasks queue `rate_limits` + bounded retry (`cloud_tasks.tf`);
  Cloud Run concurrency 1 + max-instances (`cloud_run.tf`); budget alert.
- Per-user: forward daily cap (`_within_daily_cap`); auto-submit per-user
  entitlement reservation cap.
- **Missing:** per-user/per-alias/per-source-domain **inbound** ingest rate limit
  on `/slot`+`/commit`; per-domain / per-ATS **dispatch** limiter. → **Finding
  P1-RATE-IN (inbound) + P2-RATE-ATS (per-domain).**

### C14 — Global + per-adapter kill switches
**PARTIAL.** Global kill-switches exist and are honored:
`AUTO_SUBMIT_ENABLED` re-checked inside `presubmit_recheck` (stops in-flight
before the click), `CLOUD_RUN_EXECUTOR_ENABLED`, `APPLY_MAILBOX_ENABLED`,
`AUTO_APPLY_RESERVE`. Per-adapter safety exists only as the **in-process**
`CircuitBreaker` (drift-triggered, resets on cold start) — not an operator-flippable
config kill-switch per family. → **Finding P1-KILL.**

### C15 — No sensitive screenshots / redaction
**MISSING / gap.** `worker.py` uploads `page.screenshot(full_page=True)` as the
proof receipt — a full-page capture of a **filled** ATS form including name,
contact, work-auth, and any EEO/legal/salary fields, with no redaction. Stored
via `issue_receipt_put` (see C8 encryption gap). → **Finding P1-SHOT.**

### C16 — No auto-retry after the final click
**IMPLEMENTED.** After the Submit click, a timeout/lost connection is
`submission_unknown` (`worker.py` `_SubmissionUnknown`) and is **held, never
retried** on the AWS side (`finalize_submission`; `cloud_tasks.tf` DLQ note).
Adapters never auto-retry an ambiguous submit (`ats_adapters.submit` returns
`submission_unknown` and stops). Terminal callback is one-use.

### C17 — Non-mutating approval GET + CSRF-protected POST
**PARTIAL / UNVERIFIED.** The reviewed forward deep links are token-less and
land on an auth-gated SPA page (`mail_forward_service`), and `ledger.approve` is
owner-scoped + idempotent. The **email magic-link approve action** itself lives in
`services/jobs/blueprints/apply.py` (defect J), outside this review's file set —
its GET-vs-POST + CSRF + single-use-token posture must be confirmed. → **Finding
P1-APPROVE-GET.**

---

## Prioritized findings (input to the controls-implementation task)

### P0 — must fix before any prod flag flip

**P0-1 · Browser navigation has no SSRF guard (host allow-list + IP-range block +
request interception).**
- Where: `infra/gcp/cloud-run-worker/worker.py` (`_run` / new `_Callback`-adjacent
  guard), `shared/python/services/auto_apply_service.py::execution_envelope`
  (validate `apply_url` before it leaves AWS), `shared/python/services/ats_adapters.py`
  (`open`).
- Add: (a) in `execution_envelope`, reject any `apply_url` that is not `https://`
  and whose host does not match a suffix-boundary allow-list of supported ATS
  domains — return an envelope that fails the run cleanly (`unsupported`) rather
  than dispatching. (b) In `worker.py`, install a Playwright `context.route("**")`
  interceptor that, on **every** request/redirect/subresource/popup/download,
  re-resolves the host and **aborts** if it is non-HTTPS, a non-allow-listed host,
  or resolves to private/loopback/link-local/metadata/reserved IPv4+IPv6 (guard
  against DNS rebinding by pinning/validating the resolved IP, not just the name);
  deny `file:`/`data:`/`blob:`/custom schemes.
- Threats closed: T13, T14, C1–C5.

**P0-2 · Cloud Run has no network-egress restriction / metadata block.**
- Where: `infra/gcp/terraform/cloud_run.tf` (+ a new VPC connector + egress
  firewall resource; hand to devops-engineer).
- Add: route worker egress through a VPC connector with an egress firewall that
  allows only the ATS domains + the AWS callback host and **denies**
  `169.254.169.254` / `metadata.google.internal` / RFC1918 / link-local. Enable
  metadata concealment. This is the infra half of P0-1 (defense-in-depth even if
  the interceptor is bypassed).
- Threats closed: T13, T14, C4.

### P1 — fix before the execution/mailbox planes carry real traffic

**P1-ENC · Audit receipt (and résumé GET) not pinned to a CMK / explicit bucket.**
- Where: `shared/python/services/executor_capability_service.py`
  `issue_receipt_put` (pass `kms_key_id` for the audit CMK + explicit bucket),
  `issue_resume_get` (explicit bucket). Confirm the default bucket enforces SSE.
- Threat: T-CRED-adjacent / data-at-rest for PII screenshots (C8, C15).
- **RESOLVED.** `issue_receipt_put` now presigns with `kms_key_id`
  (`_executor_kms_key_id` → `APPLY_EXECUTOR_KMS_KEY_ID` ‖ `APPLY_MAIL_KMS_KEY_ID`)
  and an explicit `_audit_bucket()`, so the receipt PUT's SIGNED policy carries
  the SSE-KMS headers the worker must echo (mirrors the mail raw-MIME PUT).
  `issue_resume_get` takes the explicit audit bucket when configured. Covered by
  `tests/test_approval_link_csrf.py::test_receipt_presign_carries_sse_kms_header`.

**P1-SHOT · Full-page unredacted proof screenshot.**
- Where: `infra/gcp/cloud-run-worker/worker.py` (screenshot step).
- Add: capture a **bounded confirmation region** (or the confirmation node) rather
  than `full_page=True`; or redact known sensitive field regions before upload; or
  store only a text/DOM confirmation receipt (`ats_adapters.receipt()` already
  produces a structured, PII-free receipt — prefer it). Coordinate with
  data-scientist on redaction. (C15.)

**P1-RATE-IN · No inbound ingest rate limit.**
- Where: `services/resume/blueprints/mail_inbound.py` (slot) + a helper in
  `shared/python/services/mail_store_service.py`.
- Add: an atomic per-alias + per-source-domain daily/burst counter (same idiom as
  `mail_forward_service._within_daily_cap`); over cap → 429 (never 401) and the
  Worker rejects with a 4xx. (C13, T10.)

**P1-KILL · No per-adapter operator kill-switch.**
- Where: `shared/python/services/ats_adapters.py` (`supported_reason` / a new
  config read) + `shared/python/services/auto_apply_service.py::presubmit_recheck`.
- Add: a config-driven per-family disable (e.g. `AUTO_APPLY_ADAPTERS_DISABLED`
  csv) re-checked in `presubmit_recheck` so an operator can dark a single ATS
  without a deploy; the in-process CircuitBreaker stays as the automatic layer.
  (C14.)

**P1-FWD-TRUST · Forwarding fires for unverified/uncorrelated mail.**
- Where: `shared/python/services/mail_forward_service.py::forward_notification`.
- Add: gate the send on the stored message/thread `trust == "verified"` (or, for
  `unverified`, drop the attacker-controlled subject/employer and send a neutral
  "a new message arrived, open in app" notice). Aligns with D8. (T15.)

**P1-APPROVE-GET · Verify email-approve link is non-mutating GET + CSRF POST +
single-use token.**
- Where: `services/jobs/blueprints/apply.py` (email approve/review action) — not
  in this review's set; audit + close.
- Add (if gap): the emailed link is a GET that renders a confirm page; the actual
  approve is a CSRF-protected POST carrying a single-use, owner-bound token. (C17,
  T4.)
- **RESOLVED (gap confirmed + fixed).** The pre-fix GET performed the approval
  (mutating on GET). Now `GET /api/apply/approve-link` is NON-mutating: it peeks
  the token (`approval_token_service.verify` — no single-use claim) and 302s to
  the app (a `review` link to the applications view; an approve/skip link to
  `/next/applications?approve=<token>`). The mutation is a new JWT-authed,
  CSRF-safe `POST /api/apply/approve-link` (authorization is a Bearer header, not
  an ambient cookie) that `verify_and_claim`s the single-use token, binds its
  owner to the JWT identity (mismatch → 403), and performs the approve/skip. All
  business failures 400/403/409 — never 401. Covered by
  `tests/test_approval_link_csrf.py`.

### P2 — harden before GA

**P2-AUDIT · Enforce audit-log immutability.** IAM-deny update/delete on the
`auto_submit_audit` path (or a WORM/TTL-free retention policy); document
append-only. Where: external `infra/terraform/` + a note in
`application_ledger_service`. (C9.)

**P2-RATE-ATS · Per-domain / per-ATS dispatch limiter.** Token bucket keyed on
the target ATS tenant at dispatch (`cloud_run_executor.submit` /
`auto_apply_service`). (C13, T-RATE.)

**P2-HMAC-REPLAY · Add timestamp+nonce to the inbound HMAC canonical.** Reject
skew and replays defensively (idempotency already neutralizes the impact). Where:
`worker.js` + `mail_inbound.py` canonical strings + `webhook_hmac`. (T1, T-REPLAY.)

**P2-IAM · Verify AWS-side least-privilege.** Confirm the resume Lambda role
restricts S3 to the résumé prefix + `auto-apply-audit/*` and KMS to the vault CMK
+ mail CMK; no bucket-wide grant. Where: external `infra/terraform/`. (C7.)

**P2-LOG · Confirm X-Ray / request logging excludes the `/executor/credential`
response.** Where: `services/resume/app.py` (X-Ray init) + gateway access-log
config. (C6.)

**P2-DKIM · Require DKIM/DMARC=pass for a `verified` correlation upgrade** on the
OTP/status path (defense-in-depth over domain+timing). Where:
`mail_store_service.correlate`. (T2.)
