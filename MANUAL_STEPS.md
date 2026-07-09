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

## Anticipated gates for later epics (built behind flags as we reach them)

| Epic | Human gate |
|---|---|
| Billing (§6.10) | Stripe account + API keys; products/prices; webhook secret. Enforcement stays behind `BILLING_ENFORCEMENT` (default false). |
| WhatsApp digest (§2.5) | Meta Business verification + WhatsApp Cloud API + template approval (days). Notifications fall back to email/push until then. |
| SMS (§2.5) | Twilio account + number + keys. |
| Mail-ingress Track A (§2.4) | A receiving domain (e.g. inbox.<domain>) with SES inbound + MX records + an S3/Lambda hook. |
| Gmail OAuth Track B (§2.4) | Google OAuth verification + CASA Tier 2 assessment. Behind `GMAIL_OAUTH_ENABLED`. |
| LinkedIn posting (§2.2) | "Share on LinkedIn" product approval for `w_member_social`. (Connect flow already built.) |

## Compass epics shipped this build — their human gates

| Epic | Human step |
|---|---|
| Sponsorship intel (§6.3) | Run `python scripts/ingest_sponsorship.py --lca <DOL LCA file> --uscis <USCIS hub csv>` (files downloaded out-of-band: DOL OFLC LCA disclosure + USCIS H-1B Employer Data Hub). Run `migrate_indexes.py` first. Until then every job's sponsorship badge is `null` (honest "no data"), not wrong. |
| Notifications / digests (§2.5) | Email works today (reuses SES). WhatsApp (`WHATSAPP_ENABLED` + Meta verification + template approval), SMS (`TWILIO_ENABLED` + Twilio), web-push (`WEBPUSH_ENABLED` + VAPID keys + `pywebpush` in requirements + a FE service worker) are built behind flags. The morning/evening **digest scheduler** is an EventBridge rule → Lambda (host it where `email_service` is importable, e.g. the auth Lambda, or promote `email_service` to the shared layer). |
| Signal bandit (§6.7) | The nightly bandit posterior update (`post_metrics_service.nightly_update()`) needs an EventBridge daily schedule → Lambda (idempotent per post; safe to double-fire). |
| Extension (§6.4) | Live-browser testing of adapter selector accuracy per ATS (Workday wizard, Ashby, new/classic Greenhouse), Review-Card isolation, CAPTCHA detection. Optional: a `POST /api/apply/field-map` endpoint to enable the (default-off) LLM field disambiguation. |
| Referral Finder (§6.11.3) | None — Premium+ gated; works off the user's own uploaded LinkedIn archive. |
| Pipeline cohort (§6.8) | Computes on-read now (bounded scan). At scale, add a nightly `pipeline_cohort_stats` aggregation job. |

## Deploy note
Both repos auto-deploy `main` on push. Backend deploy is path-scoped: `shared/**`
redeploys all four Lambdas; `services/<svc>/**` redeploys that one. The only
pre-deploy gate is an `import app; create_app()` smoke test per changed service.
