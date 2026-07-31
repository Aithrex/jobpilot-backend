# GCP browser-execution plane — setup & ops runbook (defect P5)

The auto-apply submit path can run on a **private GCP Cloud Run Playwright
worker** instead of (or alongside) the AWS Lambda browser fleet. This runbook
covers the architecture, the exact provisioning steps, the config/SSM vars, cost,
observability, and rollback.

## Architecture

```
AWS resume Lambda (orchestrator: ledger + autonomy + vault)
  └─ AUTO_APPLY_EXECUTOR=cloud_run → CloudRunExecutor.submit()
       ├─ mint one-use capability (executor_capability_service)  [AWS/Mongo]
       └─ WIF (Lambda role → GCP enqueuer SA, NO static key)
            └─ Cloud Tasks queue
                 └─ OIDC → PRIVATE Cloud Run worker  [GCP]
                      └─ callback surface (HTTPS, capability/callback-token authed)
                           /claim /resume-url /credential /receipt-url
                           /precheck /milestone /terminal
                         → back to AWS /api/resume/executor/*  [rides existing edge]
```

- The Cloud Task body carries **only** `{execution_id, capability}` — no answers,
  no résumé, no credentials, no PII.
- The worker holds **no** Mongo / SSM / bucket-wide S3 / general KMS / reusable
  AWS creds / whole mailbox. It pulls each thing it needs owner-scoped, one-use,
  short-TTL from the callback surface.
- **No new prod edge route.** The callback surface is mounted at
  `/api/resume/executor/*`, so it rides the existing `ANY /api/resume/{proxy+}`
  Terraform route (like `/api/resume/mail-inbound/*`). A *new* top-level
  `/api/<x>` prefix would 404 in prod without an external Terraform route — this
  design avoids that. Confirmed.

## Files
- AWS enqueue: `shared/python/services/cloud_run_executor.py`
- AWS capability/callback: `shared/python/services/executor_capability_service.py`
- AWS routes: `services/resume/blueprints/executor.py` (registered in
  `services/resume/app.py` under `/api/resume/executor`)
- AWS finalize + pre-submit gate: `shared/python/services/auto_apply_service.py`
  (`presubmit_recheck`, `finalize_submission`, `execution_envelope`,
  `pinned_resume_s3_key`)
- GCP IaC: `infra/gcp/terraform/`
- Worker container: `infra/gcp/cloud-run-worker/`

## Provisioning
Follow `infra/gcp/cloud-run-worker/README.md` steps 0–5 (enable APIs → terraform
apply → build/push image → `gcloud run deploy` → smoke `/healthz`). Then wire the
AWS side below.

### GCP APIs that must be enabled (in addition to the base set in the README)
The egress lockdown (below) adds two APIs to the `gcloud services enable` list:
- `vpcaccess.googleapis.com` — Serverless VPC Access (the egress connector).
- `compute.googleapis.com` — Compute Engine (VPC network, subnet, firewall
  rules, Cloud Router + Cloud NAT).
Enable them **before** `terraform apply`, or the connector/network resources fail
to create.

## Egress lockdown (audit finding P0-2)
The worker runs a real browser against attacker-influenced ATS URLs, so its
network egress is locked down as a backstop to the app-layer Playwright
interceptor (P0-1). Terraform (`infra/gcp/terraform/network.tf`) provisions:

- A **custom-mode VPC** (`vpc_network_name`, default `<name_prefix>-vpc`) with a
  single dedicated **/28 subnet** (`connector_cidr`, default `10.8.0.0/28`) for a
  **Serverless VPC Access connector**.
- The Cloud Run service routes **ALL** egress through that connector
  (`vpc_access { egress = "ALL_TRAFFIC" }` in `cloud_run.tf`).
- **Cloud Router + Cloud NAT** give the connector (no external IPs) a path to the
  public internet.
- **Egress firewall rules** (lower priority number wins):
  - `800` ALLOW within the connector's own /28 (keeps the connector healthy).
  - `900` DENY IPv4 `169.254.0.0/16` (incl. the metadata server
    `169.254.169.254`), `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
    `127.0.0.0/8`.
  - `910` DENY IPv6 `fd00::/8`, `fe80::/10`, `::1/128`.
  - `1000` ALLOW the public internet `0.0.0.0/0` (so the worker still reaches ATS
    sites + the AWS callback host `aspirely.me`).
- An ingress rule allowing only Google's health-check ranges to the connector.

**Net effect: the worker CANNOT reach the GCP metadata server
(`169.254.169.254` / `metadata.google.internal`) or any RFC1918 / loopback /
link-local host.** It can only reach the public internet — SSRF-to-internal and
metadata-token exfil are killed at the network layer even if the in-browser
interceptor is bypassed.

> **MANUAL / ORDERING STEP (do NOT skip):** the **Serverless VPC Access
> connector must exist BEFORE the Cloud Run service is deployed/updated to point
> at it.** In a single `terraform apply` this is handled automatically (Cloud Run
> depends on the connector). But the day-2 `gcloud run deploy` in step 4 of the
> worker README (which overrides the image) **must also carry the VPC connector +
> egress settings**, or the deploy replaces the revision with one that has NO
> egress lockdown. Add to that `gcloud run deploy`:
> ```
>   --vpc-connector=<vpc_connector_name>   # from `terraform output vpc_connector_name`
>   --vpc-egress=all-traffic
> ```
> Verify after any deploy: `gcloud run services describe jobpilot-apply-executor
> --region=REGION --format='value(spec.template.metadata.annotations)'` shows the
> connector + `all-traffic`. From inside the container, a curl to
> `http://169.254.169.254/` must time out / be refused.

## Config / SSM vars (AWS side)
Set under the app SSM prefix (`/jobpilot/prod/...`) in prod, or `.env` locally.
Values in the right column come from `terraform output` in `infra/gcp/terraform`.

| Var | Purpose | Source |
|---|---|---|
| `AUTO_APPLY_EXECUTOR` | `cloud_run` selects this executor (`remote`/`stub` = fallback) | set to `cloud_run` |
| `CLOUD_RUN_EXECUTOR_ENABLED` | arm dispatch (`true`); dark by default | operator |
| `AUTO_SUBMIT_ENABLED` | master submit kill-switch (must also be on) | operator |
| `GCP_PROJECT_ID` | GCP project | your project id |
| `GCP_TASKS_LOCATION` | queue + Cloud Run region | `us-east1` |
| `GCP_TASKS_QUEUE` | queue id | `cloud_tasks_queue_id` (last path segment) |
| `CLOUD_RUN_WORKER_URL` | worker HTTPS URL **+ `/task`** | `cloud_run_worker_url` + `/task` |
| `GCP_TASKS_INVOKER_SA` | OIDC SA for the task | `invoker_service_account_email` |
| `AUTO_APPLY_APPROVAL_TTL_HOURS` | pre-submit approval-expiry gate (default 72) | operator |

The AWS Lambda role must be able to federate into GCP (WIF, no static key). The
google-cloud-tasks client picks up an `external_account` credential from
`GOOGLE_APPLICATION_CREDENTIALS` pointing at a WIF credential-config JSON (no
private key — it references `wif_provider_resource_name` +
`enqueuer_service_account_email`). Generate it with:
```bash
gcloud iam workload-identity-pools create-cred-config \
  WIF_PROVIDER_RESOURCE_NAME \
  --service-account=ENQUEUER_SA_EMAIL \
  --aws \
  --output-file=gcp-wif-credentials.json
```
Ship that JSON with the Lambda (it is NOT a secret — it holds no key) and set
`GOOGLE_APPLICATION_CREDENTIALS` to its path. `google-cloud-tasks` must be a
dependency of the resume Lambda/layer for `AUTO_APPLY_EXECUTOR=cloud_run`
(imported lazily; absent → CloudRunExecutor degrades to unavailable = Lambda
fallback).

## Cost notes
- Cloud Run: `min-instances=0` → **zero idle cost**; billed per-request at 2 vCPU
  / 4 GiB for the seconds a submission runs. `max-instances=5` caps parallelism
  (also a per-domain politeness cap).
- Cloud Tasks: negligible (first 1M ops/month free).
- Artifact Registry: pennies for the image.
- WIF / STS: free.
- Budget: `google_billing_budget` alerts at **0.5 / 0.75 / 0.9** of
  `gcp_monthly_budget` (default $100). Tune the variable; add
  `budget_notification_channels` to page beyond the billing-admin emails.

## Observability / alerts
- Structured milestones per run (`claimed`, `form_filled`, `presubmit_checked`,
  `submit_clicked`, `receipt_uploaded`, `artifacts_deleted`) — audited on the AWS
  side (`auto_apply_executor_milestone`) and logged (no secrets/PII) on GCP.
- **Alert on**: Cloud Tasks task-attempt failures + queue depth (a task that
  exhausts `tasks_max_attempts` without a terminal report), Cloud Run 5xx/timeout
  rate, and — AWS side — ledger rows stuck in `running` past a threshold and any
  `submission_unknown` (needs human review; NEVER auto-retried).
- **DLQ**: Cloud Tasks has no native DLQ. Poison handling is layered (see
  `cloud_tasks.tf`): `max_attempts` caps redeliveries; a dropped task still can't
  lose an application because the terminal outcome drives the ledger via the
  callback, and stuck `running` rows are swept AWS-side.

## Failure semantics (important)
- A redelivered Cloud Task re-runs only the **claim**, which is one-use → 403 →
  the worker aborts. No double-submit.
- The **pre-submit re-check** (`/precheck`) is the last gate before the click:
  kill switch, user pause/revocation, approval expiry, immutable form hash +
  résumé/answer versions, entitlement reservation, adapter health, posting
  availability. Any failure aborts BEFORE Submit → clean retryable `failed`.
- After the Submit click, a timeout/lost connection → **`submission_unknown`** →
  the row is **HELD** and **never auto-retried** (duplicate-application guard).

## Rollback
1. Fastest: set `AUTO_APPLY_EXECUTOR` back to `remote` (Lambda fleet) or `stub`.
   The AWS side stops enqueuing immediately; no GCP change needed.
2. Or flip `CLOUD_RUN_EXECUTOR_ENABLED=false` (dispatch degrades to unavailable →
   rows stay approved for a later attempt).
3. Full teardown: `terraform destroy` in `infra/gcp/terraform`.
Because both `CLOUD_RUN_EXECUTOR_ENABLED` and `AUTO_SUBMIT_ENABLED` gate live
submits, a misconfig can only leave the pipeline in today's behavior — it can
never fire an unintended submission.
