# JobPilot auto-apply — Cloud Run Playwright worker

The private, credential-minimal browser worker for the GCP browser-execution
plane (defect P5). It drives ONE ATS application per request and holds **no**
durable cloud credentials — everything it needs (envelope, résumé, ATS
credential, receipt slot) it fetches from the AWS callback surface with the
one-use capability in the Cloud Task body.

- `worker.py` — entrypoint: claim → fetch → fill → **pre-submit re-check** →
  submit → receipt → terminal, with the `submission_unknown` boundary after the
  Submit click and unconditional local-artifact deletion.
- `ats_adapter.py` — the seam the P4 ATS adapters plug into (this worker does
  **not** reimplement form logic).
- `Dockerfile` — official Playwright base, non-root, gunicorn on `:8080`.
- Healthcheck / smoke: `GET /healthz`.

Related: the AWS side is `shared/python/services/cloud_run_executor.py` (enqueue)
+ `services/resume/blueprints/executor.py` (callback surface). The GCP infra is
`infra/gcp/terraform/`. Full runbook: `docs/ops/gcp-executor-setup.md`.

---

## EXACT human deploy steps (nothing here is run for you)

Placeholders: `PROJECT_ID`, `AWS_ACCOUNT_ID`, `AWS_LAMBDA_ROLE_ARN`,
`BILLING_ACCOUNT`, `REGION` (default `us-east1`), `REPO=jobpilot-apply-executor`.

### 0. Prereqs (once)
```bash
gcloud auth login
gcloud config set project PROJECT_ID
```

### 1. Create the project + enable APIs
```bash
# (skip create if the project already exists)
gcloud projects create PROJECT_ID --name="JobPilot Apply Executor"
gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT

gcloud services enable \
  run.googleapis.com \
  cloudtasks.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  cloudbilling.googleapis.com \
  billingbudgets.googleapis.com \
  --project=PROJECT_ID
```

### 2. Provision everything via Terraform (WIF, SAs, queue, AR, Cloud Run, budget)
```bash
cd infra/gcp/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit the values
terraform init
terraform apply \
  -var="gcp_project_id=PROJECT_ID" \
  -var="gcp_billing_account=BILLING_ACCOUNT" \
  -var="aws_account_id=AWS_ACCOUNT_ID" \
  -var="aws_lambda_role_arn=AWS_LAMBDA_ROLE_ARN"
```
The first apply creates the Artifact Registry repo and a **placeholder** Cloud
Run revision (the worker image does not exist yet). Note the outputs — you feed
several into the AWS side (see the runbook's SSM/config table).

### 3. Build + push the worker image
```bash
gcloud auth configure-docker REGION-docker.pkg.dev
IMAGE="REGION-docker.pkg.dev/PROJECT_ID/REPO/worker:prod"

cd infra/gcp/cloud-run-worker
docker build -t "$IMAGE" .
docker push "$IMAGE"
```

### 4. Point Cloud Run at the pushed image
```bash
gcloud run deploy jobpilot-apply-executor \
  --image="$IMAGE" \
  --region=REGION \
  --project=PROJECT_ID \
  --no-allow-unauthenticated \
  --ingress=internal \
  --service-account="jobpilot-apply-executor-runtime@PROJECT_ID.iam.gserviceaccount.com" \
  --concurrency=1 --min-instances=0 --max-instances=5 \
  --cpu=2 --memory=4Gi --timeout=900 \
  --set-env-vars=EXECUTOR_CALLBACK_BASE_URL=https://aspirely.me/api/resume/executor
```
(Terraform `ignore_changes` on the image means this deploy will not be reverted
by a later `terraform apply`.)

### 5. Smoke test the healthcheck
The service is internal-only, so probe it from within the project (or rely on the
Cloud Run startup probe). To hit it directly with an identity token:
```bash
URL=$(gcloud run services describe jobpilot-apply-executor \
      --region=REGION --format='value(status.url)')
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/healthz"
# -> {"status":"ok","callback_configured":true}
```

### 6. Wire the AWS side (SSM) and arm dispatch
Set the SSM params in the runbook's table (`GCP_*`, `CLOUD_RUN_WORKER_URL`,
`AUTO_APPLY_EXECUTOR=cloud_run`), then flip `CLOUD_RUN_EXECUTOR_ENABLED=true`
**and** the `AUTO_SUBMIT_ENABLED` master switch. Until both are on, dispatch stays
dark and `submit()` degrades to the existing Lambda fallback.

### Rollback
Set `AUTO_APPLY_EXECUTOR` back to `remote` (Lambda) or `stub`. No GCP teardown is
required to stop submissions — the AWS side simply stops enqueuing. To tear the
plane down entirely: `terraform destroy` in `infra/gcp/terraform`.
