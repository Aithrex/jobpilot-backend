# Account deletion & data erasure (GDPR Art. 17 / CCPA)

Closes AUDIT-P1-004 / PRIV-1. This document is the operator- and user-facing
description of what happens when a user deletes their JobPilot account.

## Entry point

`DELETE /api/auth/account` — authenticated (`@jwt_required()`), requires an
explicit confirmation body `{"confirm": "DELETE"}`. Returns a flat success
payload with the `job_id` of the asynchronous purge.

## Two-phase flow: soft delete → hard purge

Deletion is split so the account is neutralized instantly while the heavy
cross-store erasure runs in the background (it touches ~50 collections + S3 +
three OAuth providers, far beyond API Gateway's 29s budget).

1. **Soft delete (synchronous, `account_service.soft_delete`).**
   - Flips `is_deleted=true`, `deleted_at`, `deletion_status="pending_purge"`,
     `status="deleted"` on the `users` document.
   - Deletes any outstanding `password_reset_tokens` so the account cannot be
     revived via a stale reset link.
   - The route returns immediately; the frontend clears the token and logs the
     user out (JWTs are stateless — there is no server session to revoke, so a
     token already issued stays cryptographically valid until it expires; login
     must reject `is_deleted` accounts).

2. **Hard purge (asynchronous, `account_service.hard_purge`).**
   Enqueued as the `account_purge` async job and processed by the resume
   Lambda's `_process_job`. Idempotent, fail-soft per store, and logs a
   redacted audit trail (the email is masked in every log line).

## Retention window

The purge is enqueued **immediately** after soft-delete; under normal operation
hard-purge completes within seconds to a few minutes. If the async job fails
mid-run, it is safe to re-run (idempotent) — every step converges to the same
empty state. There is no grace/undo period once confirmed; treat deletion as
irreversible from the moment `DELETE /api/auth/account` succeeds.

## What is erased (Mongo cascade)

Every document keyed to the account holder's email is deleted across:

- **Identity / resumes:** `users`, `user_preferences`, `user_resumes`,
  `password_reset_tokens`, `tailoring_records`, `feedback`.
- **Usage / entitlements:** `tailor_usage`, `feature_usage`,
  `entitlement_overrides`.
- **Voice Graph:** `graph_nodes`, `graph_edges`, `graph_evidence`,
  `writing_samples`, `style_profiles`, `authored_resumes`, `consents`,
  `merge_candidates`.
- **Jobs / apply / extension:** `saved_jobs`, `application_answers`,
  `application_profiles`, `job_activity_log`, `job_filter_preferences`,
  `pipeline_filter_presets`, `user_apify_keys`, `user_events`.
- **Career intelligence:** `networking_contacts`, `offer_workspace`,
  `differentiation_analyses`, `github_profiles`, `visa_profiles`,
  `career_copilot_state`, `career_copilot_outreach`, `interview_attempts`.
- **Learning / posting:** `learning_paths`, `learning_progress`, `post_drafts`,
  `post_metrics`, `scheduled_posts`, `linkedin_archives`.
- **Integrations (token docs):** `gmail_connections`, `gmail_suggestions`,
  `gmail_oauth_states`, `github_connections`, `linkedin_connections`,
  `connect_oauth_states`.
- **Mail ingress:** `mail_ingress_addresses`, `email_ingress`,
  `mail_ingress_suggestions`.
- **Notifications:** `notification_prefs`, `notification_log`,
  `notification_consents`.
- **Async job store:** `resume_jobs` (all of the user's jobs except the running
  purge job itself, so its completion can still be polled).

## What is erased (S3)

All objects under the `{user_id}/` prefix — both `{user_id}/base/…` (uploaded
résumés) and `{user_id}/generated/…` (tailored outputs). The prefix uses the
Mongo `_id`, which is why the `users` document is resolved first and deleted
last.

## Best-effort steps

- **OAuth provider revocation.** The user's Gmail, GitHub, and LinkedIn grants
  are revoked at the provider via each integration's `disconnect()`. This is
  best-effort: if the provider call fails or is unreachable, the local token
  document is still deleted by the Mongo cascade, so no credential is retained
  on our side. Outcomes are recorded in the job report (`providers`).
- **Per-store isolation.** If any single collection, the S3 purge, or a provider
  revocation raises, the error is collected into the report's `errors` list and
  the remaining stores are still purged — a partial failure never aborts the
  cascade.

## Deliberately retained (NOT erased)

- **`billing_incidents`** — financial records (payment-failure ledger) kept
  under a legal-basis retention exception (tax / chargeback obligations).
- **`audit_log`, `eval_ratings`** — internal admin-action and rater trails,
  keyed by the actor/rater, not by the account holder as data subject.
- **Global caches** (not user-keyed, contain no personal data): `jobs_cache`,
  `apify_actor_cache`, `sponsorship_signals`, `salary_benchmarks`, `events`,
  `scrape_runs`, `bandit_arms`, `bandit_updates`, `stripe_events`,
  `tech_chronicle_cache`, `career_pages_cache`, `workday_jobs_cache`.
- **`oauth_login_states`** — ephemeral (10-minute TTL), keyed by a random state
  token and holding no email; nothing to target.

## Verification

`python -m pytest tests/test_account_deletion.py -q` — offline (mongomock), no
AWS/network. Asserts the full cascade empties every keyed collection for the
target user while an unrelated user's identical rows are untouched, plus
idempotency, running-job preservation, soft-delete flagging, and per-store
fail-soft behavior.
