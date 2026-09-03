# MANUAL_STEPS — things that need a human, an account, or a key

Per the master build prompt: features that depend on an external account/approval
are built behind a flag and their human step is recorded here, never faked. Nothing
below blocks the code that ships around it.

## Voice Graph (§6.12) — current build

| Step | What | Status |
|---|---|---|
| Turn on in prod | Set `VOICE_GRAPH_ENABLED=true` on the **resume** Lambda env (or SSM) once the ship gate passes. Default is OFF in prod (routes 404), ON in local dev. | Human toggle when ready |
| Encryption key | Writing samples are Fernet-encrypted with `INTEGRATION_TOKEN_ENCRYPTION_KEY`, falling back to `GMAIL_TOKEN_ENCRYPTION_KEY`. At least one MUST be set in the resume Lambda's SSM/env or sample save fails. Note: `INTEGRATION_TOKEN_ENCRYPTION_KEY` is not in the SSM param map today, so it resolves via the Gmail key — confirm `GMAIL_TOKEN_ENCRYPTION_KEY` is present, or add `INTEGRATION_TOKEN_ENCRYPTION_KEY` to `utils/ssm_config.py`. | Verify a key is set |
| Indexes | Run `python scripts/migrate_indexes.py` against prod Mongo to create the new collections' indexes (writing_samples, consents, graph_nodes/edges/evidence, style_profiles, authored_resumes). Until then those queries COLLSCAN (fine at low volume). | Run once |
| Model tier | Extraction/authored use PRO (Sonnet 4.6), the top wired Bedrock tier. The addendum names Opus (`claude-opus-4-8`); to use it, enable that model in Bedrock and add a tier id — see ADR-002. | Optional upgrade |
| Ship gate | Human blind panel (`recruiter-panel`): authored must beat assisted and beat the original before flipping the prod flag (§6.12.9). | Before go-live |

## Opt-in auto-submit (§ guardrailed apply) — current build

The shipped default is the **never-submit Review Card**: the extension fills a form
and stops. Auto-submit is inert until an operator flips it, and even then only ever
fires with explicit, per-application user approval (undo window + single-use,
expiring approval record). Backend is complete behind the flag; the WhatsApp
approval channel's carrier plumbing is the human gate below.

| Step | What | Status |
|---|---|---|
| Master kill-switch | Set `AUTO_SUBMIT_ENABLED=true` on the **jobs** Lambda env/SSM to expose the feature. Default is OFF in prod (every `/api/apply/auto-submit/*` route 404s), ON in local dev — same resolution as `VOICE_GRAPH_ENABLED` / `MAIL_INGRESS_ENABLED` / `GMAIL_OAUTH_ENABLED` (`auto_submit_service.auto_submit_enabled()`). | Human toggle when ready |
| Per-user opt-in | Separate from the master flag: each user must set `auto_submit_enabled=true` via `PUT /api/apply/auto-submit/settings` (default False). BOTH must be true to auto-submit. | User action |
| Indexes | Run `python scripts/migrate_indexes.py` to create indexes for `auto_submit_settings`, `auto_submit_approvals`, `auto_submit_audit` (specs added this session). Until then those queries COLLSCAN (fine at low volume). | Run once |
| **WhatsApp approval channel** | The backend hook `auto_submit_service.create_whatsapp_approval(user_email, application_id, answers)` creates an `approved` record with `source: whatsapp`, carrying the SAME guardrails as in-app (master flag + per-user opt-in + undo window + single-use expiry). The **Twilio / WhatsApp Cloud API send + receive is NOT built** and is not faked: it needs a Meta Business verification + WhatsApp Cloud API number + an approved message template (days), and Twilio (account + number + keys) if SMS-backed. The inbound reply handler is a NEW top-level prefix (e.g. `POST /api/whatsapp-inbound`) that needs an **external Terraform edge route** + Twilio/Meta signature verification; only after verifying an approving reply does it call `create_whatsapp_approval(...)`. Until wired, auto-submit approvals arrive `in_app` only. | Meta/Twilio account + verification + template approval + edge route |

## Guest Onboarding — current build

| Step | What | Status |
|---|---|---|
| **`GUEST_IP_HASH_SALT` (new SSM secret)** | Server-side salt for `guest_sessions.ip_hash` (sha256(ip + salt) for abuse correlation — never the raw IP). **Unset → fail closed:** `ip_hash` stored as `null`, feature still works, one no-PII warning logged. To wire it, the standard **4-edit SSM pattern** applies — the two in-repo edits are ✅ done (`utils/ssm_config.py` param map entry `SSM_GUEST_IP_HASH_SALT` + `SSMConfig.GUEST_IP_HASH_SALT` property); remaining for devops: (1) create the SecureString parameter (e.g. `/portfolio/prod/guest-ip-hash-salt`, value = a long random string), (2) add the `SSM_GUEST_IP_HASH_SALT` env var (pointing at that parameter path) to the **resume** Lambda in the external Terraform (jobs Lambda too if it ever calls `hash_ip`). Locally: set `GUEST_IP_HASH_SALT` in `.env`. | Devops: SSM param + Lambda env |
| `TRUSTED_PROXY_HOPS` | `utils/security.get_client_ip` now derives the client IP from the rightmost trustworthy X-Forwarded-For entry. Default `1` matches prod (CloudFront → API Gateway). If API Gateway is ever exposed WITHOUT CloudFront, set `TRUSTED_PROXY_HOPS=0` on every Lambda so only the connection source IP is trusted. | Only on topology change |
| Global daily guest budgets | `global_daily_guest_parses` (500) and `global_daily_guest_matches` (1000) join `global_daily_guest_tailors` (200) in `platform_config` — admin-tunable at runtime via `PUT /api/admin/guest-config`, no deploy. | Optional tuning |
| **`GUEST_SIGNAL_HMAC_KEY` (new SSM secret, v2)** | Keyed-HMAC secret for guest identity signals (`identity.email_hmacs/phone_hmacs/resume_sig_hmacs` — stored as `"v1:" + hex(hmac_sha256(key, value))`, never plaintext). **Unset → fail closed:** signals are not stored or matched, quota subjects stay standalone (each browser/session gets its own trial), one no-PII warning logged, feature keeps working. In-repo edits ✅ done (`utils/ssm_config.py`: param-map entry `SSM_GUEST_SIGNAL_HMAC_KEY` + `SSMConfig.GUEST_SIGNAL_HMAC_KEY`). Devops: (1) create the SecureString parameter (e.g. `/portfolio/prod/guest-signal-hmac-key`, long random value — NEVER the JWT secret), (2) add env `SSM_GUEST_SIGNAL_HMAC_KEY` (the parameter path) to the **resume AND jobs** Lambdas (both run identity/session code). Locally: `GUEST_SIGNAL_HMAC_KEY` in `.env`. Do NOT rotate casually — rotation orphans stored signals (subjects fall back to standalone until re-seen). | Devops: SSM param + Lambda env |
| **`GUEST_DOWNLOAD_TOKEN_SECRET` (new SSM secret, v2)** | HMAC secret for the 15-minute guest download capability tokens (versioned HMAC over `record_id\|guest_id\|format\|exp`). **Unset in prod → fail closed:** token mint/verify answer with a 503-style capacity message (never 401); dev falls back to a per-process secret. In-repo edits ✅ done (`SSM_GUEST_DOWNLOAD_TOKEN_SECRET` + `SSMConfig.GUEST_DOWNLOAD_TOKEN_SECRET`). Devops: create `/portfolio/prod/guest-download-token-secret` (SecureString, distinct random value — NEVER `JWT_SECRET_KEY`) and add env `SSM_GUEST_DOWNLOAD_TOKEN_SECRET` to the **resume** Lambda. Locally: `GUEST_DOWNLOAD_TOKEN_SECRET` in `.env`. | Devops: SSM param + Lambda env |
| **Retention schedule (v2)** | Guest retention is a claim-aware JOB, not a TTL: EventBridge rule `guest_retention` (added in `infra/terraform/schedulers.tf`, daily 05:45 UTC) → resume Lambda `{"scheduled_job": "guest_retention"}` → `scheduled_jobs.dispatch_scheduled` → async `guest_retention_cleanup` `_process_job` branch. Deletes ONLY `guest_sessions` with `claimed_by: null` older than the admin `retention_days` knob (default 30; 0 = keep forever → job reports skipped) plus their unclaimed `tailoring_records` and guest `resume_jobs`. Also admin-invokable on demand: `POST /api/admin/guest-retention-cleanup` → `{job_id}` (poll `GET /api/resume/job/<id>`). Devops: `terraform apply` picks up the new rule. | Terraform apply |
| **v2 indexes + identity backfill** | Run in this order against prod Mongo: (1) `python scripts/migrate_indexes.py` — adds the v2 indexes (`quota_subject_id`, HMAC signal fields, `device_token_hash`, `guest_quota_subjects.subject_id` unique, `guest_claim_carryins (subject_id,date)` unique, `retention_scan`) and DROPS the v1 plaintext identity indexes; (2) `python scripts/backfill_guest_identity_v2.py` (one-shot, idempotent; requires `GUEST_SIGNAL_HMAC_KEY`) — mints one quota subject per existing merged cluster (linkage is NOT re-run on plaintext), sums `tailors_used` onto the subject, re-keys plaintext identity arrays into HMACs, then `$unset`s the plaintext. Use `--dry-run` first. | Run once |

## Anticipated gates for later epics (built behind flags as we reach them)

| Epic | Human gate |
|---|---|
| Billing (§6.10) | Stripe account + API keys; products/prices; webhook secret. Enforcement stays behind `BILLING_ENFORCEMENT` (default false). |
| WhatsApp digest (§2.5) | Meta Business verification + WhatsApp Cloud API + template approval (days). Notifications fall back to email/push until then. |
| SMS (§2.5) | Twilio account + number + keys. |
| Mail-ingress Track A (§2.4) | A receiving domain (e.g. inbox.<domain>) with SES inbound + MX records + an S3/Lambda hook. |
| Gmail OAuth Track B (§2.4) | Google OAuth verification + CASA Tier 2 assessment. Behind `GMAIL_OAUTH_ENABLED` (kill-switch in `gmail_service.gmail_oauth_enabled()`; default OFF in prod, ON in dev; gates the `gmail.readonly` consent/callback/sync routes independent of credential presence). Flip to `true` only after the CASA assessment passes. |
| **Google sign-in (login)** | Fully built end-to-end (`oauth_service.py` provider `google` + `/api/auth/oauth/google/authorize` + `/oauth/callback`). It only ADVERTISES + works once you set **`GOOGLE_SIGNIN_CLIENT_ID`** + **`GOOGLE_SIGNIN_CLIENT_SECRET`** (a Google Cloud OAuth 2.0 Web client; authorized redirect URI = the app's `/oauth/callback`) in the backend config (SSM in prod, `.env` locally). `provider_configured("google")` gates it; until set, `/authorize` 503s and the FE's "Continue with Google" button shows an honest "being set up" toast. This is SEPARATE from the Gmail-readonly creds above. |
| LinkedIn posting (§2.2) | "Share on LinkedIn" product approval for `w_member_social`. (Connect flow already built.) |

## Compass epics shipped this build — their human gates

| Epic | Human step |
|---|---|
| Sponsorship intel (§6.3) | Run `python scripts/ingest_sponsorship.py --lca <DOL LCA file> --uscis <USCIS hub csv>` (files downloaded out-of-band: DOL OFLC LCA disclosure + USCIS H-1B Employer Data Hub). Run `migrate_indexes.py` first. Until then every job's sponsorship badge is `null` (honest "no data"), not wrong. |
| Notifications / digests (§2.5) | Email works today (reuses SES). WhatsApp (`WHATSAPP_ENABLED` + Meta verification + template approval), SMS (`TWILIO_ENABLED` + Twilio), web-push (`WEBPUSH_ENABLED` + VAPID keys + `pywebpush` — ✅ in layer requirements, deploys with next push — + a FE service worker) are built behind flags. The morning/evening **digest scheduler** is an EventBridge rule → Lambda (host it where `email_service` is importable, e.g. the auth Lambda, or promote `email_service` to the shared layer). |
| Signal bandit (§6.7) | The nightly bandit posterior update (`post_metrics_service.nightly_update()`) needs an EventBridge daily schedule → Lambda (idempotent per post; safe to double-fire). |
| Extension (§6.4) | Live-browser testing of adapter selector accuracy per ATS (Workday wizard, Ashby, new/classic Greenhouse), Review-Card isolation, CAPTCHA detection. Optional: a `POST /api/apply/field-map` endpoint to enable the (default-off) LLM field disambiguation. |
| Referral Finder (§6.11.3) | None — Premium+ gated; works off the user's own uploaded LinkedIn archive. |
| Pipeline cohort (§6.8) | Computes on-read now (bounded scan). At scale, add a nightly `pipeline_cohort_stats` aggregation job. |
| Mail-ingress Track A (§2.4) | A receiving domain `inbox.<domain>` (MX → SES inbound) + an SES receipt rule → SNS → a signature-verified `POST /api/mail-inbound/ses` Lambda (new prefix = external Terraform route). Set `MAIL_INGRESS_DOMAIN` + flip `MAIL_INGRESS_ENABLED=true`. Nightly `purge_expired()` schedule. Default OFF → the 3 FE routes 404. |
| Billing / Stripe (§6.10) | ~~Add `stripe>=9.0.0` and `PyYAML>=6.0` to `shared/python/requirements.txt`; package `config/` into the layer~~ ✅ done in repo — deploys with next push (`build_layer.sh` now ships `config/plans.yaml` at `/opt/python/config/`, resolved by `stripe_service._plans_yaml_candidates()`). Remaining human gates: set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO/CAREER`, and `BILLING_ENFORCEMENT=true`; register `POST /api/billing/webhook` in Stripe for `checkout.session.completed` / `customer.subscription.*` / `invoice.payment_failed`. Dark until then (503). |

## Learning loop (§6.6) — gates

| Step | What |
|---|---|
| Auto-post scheduler | `learning/publish.py` schedules a verified-win LinkedIn post 30 min out (cancellable). Nothing fires it yet — add an EventBridge daily/5-min rule → a worker that calls `publish.send_scheduled_post` for `scheduled_posts` where `status=="scheduled" and scheduled_for<=now`. Until then, auto-post = scheduled-but-not-sent (honest, never a surprise). Manual publish works today. |
| Extract the `learning` Lambda (optional, ADR-003) | The `services/learning/` skeleton is deploy-ready + inert. To make it its own service: (1) add `learning: ['services/learning/**']` to `.github/workflows/deploy.yml` filters + the resolve step; (2) add `learning = { routes = ["/api/learning"], memory = 1024, timeout = 60 }` to `infra/terraform/variables.tf` `service_config` (the `for_each` auto-creates the function + `ANY /api/learning` + `/{proxy+}` routes); `terraform apply`. Keep async learning routes proxied via resume, or give the learning handler its own self-invoke branch. |
| Indexes | `python scripts/migrate_indexes.py` also creates `differentiation_analyses`, `scheduled_posts`, and the graph/learning indexes. |

## Deploy note
Both repos auto-deploy `main` on push. Backend deploy is path-scoped: `shared/**`
redeploys all four Lambdas; `services/<svc>/**` redeploys that one. The only
pre-deploy gate is an `import app; create_app()` smoke test per changed service.

## 2026-09-03 — `jobpilot-ci-ingest-runner-deploy` (RESOLVED, kept for the lesson)

The `deploy-ingest-runner` CI job needs three grants the CI user did not have:
`s3:PutObject` on `deploy/ingest-runner/*`, `ssm:SendCommand` on the one
instance and the one document, and `ssm:GetCommandInvocation`. Its first run
failed with `AccessDenied` on PutObject.

Applied by CLI first, then **imported into state — `terraform plan` now reports
no differences.** Nothing outstanding.

> **The detour is the part worth keeping.** `terraform plan` first refused with
> *"Backend configuration block has changed"*, which reads as state drift and
> sent me looking for a migration problem. It was neither drift nor
> permissions: `versions.tf` declared `required_version = ">= 1.5"` while the
> S3 backend uses `use_lockfile`, which is **Terraform >= 1.10**. A 1.9.8
> binary cannot parse it, and says so as a backend error before it ever says
> so as a version error. The constraint is now `>= 1.10`, so the next person
> gets one clear message instead of two misleading ones.

If you ever need to re-do the import:

```bash
cd infra/terraform
terraform init -reconfigure     # safe: same bucket and key, -reconfigure never copies state
terraform import aws_iam_user_policy.ci_ingest_runner_deploy \
    jobpilot-ci-deployer:jobpilot-ci-ingest-runner-deploy
```

## 2026-09-03 — OPEN: `median_tailored` snapshot is failing on a real regression

`tests/test_resume_layout.py::test_visual_snapshots` fails on
`median_tailored`. **Do not regenerate the golden to clear it.** The test is
correct and the render is wrong.

The renderer says so itself:

```
resume_render page_mismatch solver=1 actual=2 h=277.5 avail=265.7
```

Its fit solver predicts ONE page; the actual render is TWO. The committed
golden (from `bcab6ca`) fits EDUCATION on page 1; after `ce9a94b` the content
overflows by ~11.8mm and EDUCATION's heading is orphaned at the foot of page 1
with its body on page 2.

**Mechanism.** `ce9a94b` raised several fixed spacing constants and, decisively,
froze the solver's only compression lever:

```
min_post_header       0.33 -> 0.80      # both ends now 0.80: no range left
max_post_header       0.50 -> 0.80      # commented "(frozen)"
heading_cell_h        4.06 -> 4.70
contact_section_gap_mm 0.88 -> 1.82
```

With `min == max` the solver cannot compress header spacing at all, so a
résumé it still predicts as one page renders as two.

**Two defensible fixes, both design calls for whoever owns the renderer:**

1. give the solver a range again (keep 0.80 as the *max*, restore a smaller
   min) so it can compress to fit, preserving the reference look when there is
   room; or
2. accept two pages -- but then teach the solver so `page_mismatch` stops
   firing, and add keep-with-next for section headings so EDUCATION never
   orphans.

Not fixed here because both change the reference-matching intent that
`ce9a94b` set deliberately.

> **This blocks deploys now**, by design: `deploy.yml`'s `await-tests` gate
> refuses to deploy from a red main. Override for an urgent ship with
> `gh workflow run deploy.yml --ref main`, which bypasses the gate and logs a
> warning saying so.
