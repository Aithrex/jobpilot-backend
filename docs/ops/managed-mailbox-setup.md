# Managed mailbox — external setup runbook (Cloudflare + S3)

The go-live steps that **cannot be done from this repo** for the managed
application mailbox inbound path. Contract + rationale live in
[`docs/specs/managed-mailbox-v1.md`](../specs/managed-mailbox-v1.md); this doc is
the operator checklist matching that spec's §11/§12.

Everything below is **held OFF in prod** until every step is done and the
staging round-trip passes: the backend keeps `APPLY_MAIL_DOMAIN=""` and
`APPLY_MAILBOX_ENABLED="false"` (`infra/terraform/lambda.tf`) so the feature is
inert until flipped.

## What is already in-repo (code + IaC, not yet live)

| Piece | Location |
|---|---|
| Cloudflare Email Worker (thin two-phase handoff) | `infra/cloudflare/email-worker/` |
| S3 bucket + KMS CMK + least-privilege IAM | `infra/terraform/managed_mailbox.tf` |
| New TF vars (`mail_bucket_name`, `mail_raw_retention_days`) | `infra/terraform/variables.tf` |
| Lambda env wiring (`APPLY_MAIL_BUCKET`, `APPLY_MAIL_KMS_KEY_ID`, `SSM_MAIL_INBOUND_HMAC_SECRET`) | `infra/terraform/lambda.tf` |

> **No new API-Gateway/edge route is required.** The machine webhook rides the
> already-routed `/api/resume/{proxy+}` edge and raw MIME goes directly to S3 via
> a presigned PUT, never through API Gateway (spec §4).

## Runbook (do in order)

### 1. Register the neutral domain
Register a neutral domain we control — **not** aithrex.tech / aspirely.me /
jobpilot (spec D3). The address is shown to employers and must read as the
candidate's private email. The operator provides the name; it becomes
`APPLY_MAIL_DOMAIN`.

### 2. Apply the AWS infrastructure (S3 + KMS + IAM)
From `infra/terraform/` with `AWS_PROFILE=aithrex`:

```bash
cd infra/terraform
terraform plan    # REVIEW: adds aws_s3_bucket.mail, aws_kms_key.mail,
                  # aws_iam_role_policy.mail + the two lambda env vars.
                  # It must ADD only — nothing destroyed/replaced.
terraform apply
```

Optional: pin an exact bucket name with `-var 'mail_bucket_name=jobpilot-mail-prod'`
or change retention with `-var 'mail_raw_retention_days=180'`. Note the outputs
`mail_bucket_name`, `mail_kms_key_arn` (the plan wires these into the Lambda env
automatically — no manual SSM entry for the key id; it is not a secret).

### 3. Seed the shared HMAC secret in SSM
Generate a strong random secret and store it as a SecureString at the path the
Lambda already reads (`SSM_MAIL_INBOUND_HMAC_SECRET`):

```bash
SECRET=$(openssl rand -hex 32)
aws ssm put-parameter --profile aithrex \
  --name /jobpilot/prod/mail-inbound-hmac-secret \
  --type SecureString --value "$SECRET"
# keep $SECRET — you paste the SAME value into the Worker in step 5.
```

### 4. Add the domain to Cloudflare + enable Email Routing
- Add the domain as a Cloudflare site; switch its nameservers.
- Email → Email Routing → enable (Cloudflare auto-adds MX + SPF). Wait for MX
  to go active.

### 5. Deploy the Email Worker + bindings
```bash
cd infra/cloudflare/email-worker
npm install
npx wrangler login
npx wrangler secret put MAIL_INBOUND_HMAC_SECRET   # paste $SECRET from step 3
# confirm MAIL_INBOUND_URL in wrangler.toml points at the prod backend base
npx wrangler deploy
```

### 6. Point the catch-all at the Worker
Cloudflare → Email → Email Routing → Routing rules → **Catch-all address** →
Action **Send to a Worker** → select `jobpilot-mail-inbound` → Save.

### 7. Verify the ZeptoMail relay sender
Verify `MAIL_FORWARD_FROM` (e.g. `notifications@aspirely.me`) as a sender in
ZeptoMail with SPF/DKIM passing (spec §7/§D5). Forwarding notifications are sent
from this JobPilot-owned address — never spoofing the recruiter.

### 8. Staging round-trip, then flip the flag
Send a test email to any `@<domain>` address. Confirm: Worker breadcrumbs via
`npx wrangler tail` (sizes/message-id/status only), raw object under
`mail/raw/…` in the bucket, and a thread/message in the Inbox API. When the
round-trip (send → Worker → webhook → Inbox → forward) passes, set on the
backend and redeploy:

```
APPLY_MAIL_DOMAIN     = "<the neutral domain>"
APPLY_MAILBOX_ENABLED = "true"
```

Then roll out internal → canary → beta (spec §11).

## Rotation / teardown notes
- Rotating the HMAC secret requires updating **both** the SSM value (step 3) and
  the Worker secret (step 5) together — a mismatch makes `/slot` + `/commit`
  fail closed with 403.
- The KMS CMK has a 30-day deletion window and annual rotation; the S3 bucket is
  versioned with a TLS-only policy. Raw MIME under `mail/raw/` expires after
  `mail_raw_retention_days` (default 365); message metadata lives in Mongo and is
  durable (spec §2/§6).
