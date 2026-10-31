# Auto-Apply Browser Fleet — Deploy & Validation Runbook

> **Retired 2026-10-31.** This describes the AWS setup, which has been deleted. It is kept for history. Current hosting: [`infra/compose/README.md`](compose/README.md).

The `jobpilot-apply-executor` container Lambda (Playwright + Chromium) that
actually submits applications. Everything ships **dark**; nothing submits until
the flags at the end are flipped.

## What's already been validated locally (no deploy needed)
- **Full submit loop** (fill → react-select → attach résumé → click submit →
  detect confirmation → screenshot) against Greenhouse-style form fixtures —
  `tests/test_browser_executor.py`, run with real headless Chromium.
- **CAPTCHA wall** detection (real challenge `bframe` → `WALL_WAITING_VERIFY`,
  freeze/thaw), and **validation-failure** detection (no false "submitted").
- **Live Greenhouse DOM**: text fields fill correctly, résumé attaches via the
  "Attach" file-chooser, and the passive reCAPTCHA badge is correctly NOT
  treated as a block (only a visible challenge frame is). Verified by driving a
  real apply page and stopping before submit (nothing was sent).

## Known real-world constraint — read this before enabling
Many Greenhouse forms carry **reCAPTCHA Enterprise**. Invisible/score-based
instances pass silently for a normal session, but a **headless datacenter
browser often scores low and gets challenged**. When that happens the executor
returns `WALL_CAPTCHA` → the row freezes → the hourly sweep re-drives it (and,
via the approval loop, the user can be asked to complete it). So "fully
autonomous, zero-touch submit" will have a **CAPTCHA tail** that falls back to
human. Options, in order of preference: (a) accept the freeze/thaw human
fallback; (b) run submit in the user's own browser via the extension (trusted
session, user solves their own challenge); (c) add a CAPTCHA-solver service
(cost + ToS grey). Do NOT enable autonomous submit expecting 100% hands-off.

## Deploy (all under the `aithrex` profile / us-east-1)

1. **Build + push the image** (from `jobpilot-backend/`):
   ```bash
   ACCOUNT=032398581420; REGION=us-east-1
   REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/jobpilot-apply-executor-prod
   aws ecr get-login-password --profile aithrex --region $REGION \
     | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
   # ECR repo must exist first (terraform apply of apply_executor.tf creates it).
   docker build -f services/apply_executor/Dockerfile -t $REPO:prod .
   docker push $REPO:prod
   ```
   (First time: `terraform apply -target=aws_ecr_repository.apply_executor` to
   create the repo before the push, then the full apply below.)

2. **Provision infra** (`infra/terraform/`): `terraform apply` — creates the ECR
   repo, the container Lambda (2 GB / 300 s / reserved-concurrency 5), its
   least-priv role (S3 read résumé + write proof, KMS decrypt), and grants the
   resume Lambda `lambda:InvokeFunction`. Still dark.

3. **Point the resume Lambda at the fleet** (env on `jobpilot-resume-prod`):
   - `AUTO_APPLY_EXECUTOR=remote`
   - `EXECUTOR_REMOTE_ENABLED=true`
   - `APPLY_EXECUTOR_FUNCTION=jobpilot-apply-executor-prod` (default)
   - keep `AUTO_SUBMIT_ENABLED` **unset/off** for now.

## Validate SAFELY (never submit to a stranger's live req)

Pick ONE controlled target — do **not** point this at a real company's open
posting:
- **Best:** a dummy req on a board **you** control (Aithrex's own Greenhouse/
  Lever/Ashby trial account), or
- a self-hosted copy of the form fixture (`tests/fixtures/gh_form_ok.html`)
  served over HTTPS behind a greenhouse-looking host you own.

Then, with a **temp mailbox** for the candidate identity:
1. Direct-invoke the executor against the controlled apply URL:
   ```bash
   aws lambda invoke --profile aithrex --function-name jobpilot-apply-executor-prod \
     --payload '{"method":"submit","args":{"application":{"application_id":"test1","apply_url":"<YOUR_CONTROLLED_URL>","ats_family":"greenhouse"},"answers":{"first_name":"Test","last_name":"Bot","email":"<TEMP_MAIL>"},"resume_pdf":null}}' \
     /dev/stdout
   ```
   Expect `{"status":"submitted","confirmation":...,"screenshot_ref":"auto-apply-audit/test1/....png"}`.
2. Confirm the proof screenshot landed in S3 (`RESUME_S3_BUCKET`, prefix
   `auto-apply-audit/`) and the temp mailbox received the ATS acknowledgment.
3. End-to-end via the pipeline: queue a controlled application → `gather` →
   approve (magic-link) → `request_immediate_submit` fires → verify the ledger
   row reaches `submitted`.

## Go live
Only after step 3 passes: set `AUTO_SUBMIT_ENABLED=true` (or the per-user opt-in)
on the resume + jobs Lambdas. Approvals (and clean `autonomy=="auto"` decisions)
now submit immediately via the browser fleet.

## Kill-switch
Flip `AUTO_SUBMIT_ENABLED` off (instant, no deploy) — `request_immediate_submit`
and the sweep both short-circuit. Or `EXECUTOR_REMOTE_ENABLED=false` to fall the
resume Lambda back to leaving rows `ready` without invoking the fleet.
