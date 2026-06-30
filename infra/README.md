# JobPilot Backend — Deployment

Four Lambda microservices (**auth · jobs · resume · billing**) behind one HTTP
API, sharing one Lambda layer. Built from `services/<svc>/` + `shared/python/`.

```
build_layer.sh ─► build/layer.zip ──┐
package.sh     ─► build/<svc>.zip ──┤─► terraform apply ─► 4 Lambdas + layer + HTTP API
                                     │
SSM params (secrets) ────────────────┘
```

## 1. Build artifacts (from the repo root: `jobpilot-backend/`)

```bash
bash scripts/build_layer.sh        # -> build/layer.zip  (shared code + deps, Linux wheels)
bash scripts/package.sh            # -> build/{auth,jobs,resume,billing}.zip
```

> `build_layer.sh` fetches **manylinux** wheels for `python3.12`, so it works on
> Windows/macOS. Keep `runtime` in `terraform.tfvars` matching the version arg.

## 2. Seed secrets in SSM Parameter Store (once)

Prod runs with `USE_SSM_SECRETS=true`; the services read SecureString params
under `ssm_path_prefix` (default `/jobpilot/prod`). Create at least:

```
/jobpilot/prod/MONGODB_URI
/jobpilot/prod/JWT_SECRET_KEY            # MUST equal the legacy portfolio value
/jobpilot/prod/DB_NAME
/jobpilot/prod/GMAIL_TOKEN_ENCRYPTION_KEY
/jobpilot/prod/INTEGRATION_TOKEN_ENCRYPTION_KEY
# ...plus the OAuth / API-key params your services use (see .env.example)
```

```bash
aws ssm put-parameter --name /jobpilot/prod/JWT_SECRET_KEY --type SecureString --value '...'
```

## 3. Deploy

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # fill in
terraform init
terraform apply
```

Outputs include `api_endpoint` (the HTTP API URL) and `function_names`.

## 4. Wire the edge

Point CloudFront's `/api/*` behavior at the `api_endpoint` origin (the same
distribution that serves the `aspirely.me` SPA from S3). The HTTP API already
fans `/api/<prefix>` to the right Lambda:

| Prefix | Lambda |
|---|---|
| `/api/auth`, `/api/admin` | auth |
| `/api/jobs`, `/api/apply` | jobs |
| `/api/resume` (+ `/api/resume/feedback`), `/api/tech-chronicle` | resume |
| `/api/billing` | billing |

## CI/CD (auto-deploy on push to `main`)

`.github/workflows/deploy.yml` deploys on every push: it detects which
`services/<svc>/` changed, rebuilds + republishes the **shared layer** when
`shared/**` changed (then redeploys all 4 to pick up the new layer version),
smoke-tests `create_app()`, and ships changed services via
`aws lambda update-function-code`. **Terraform owns infra; CI owns code** — the
function `lifecycle { ignore_changes = [source_code_hash, layers] }` keeps the
two from fighting.

Required GitHub Actions secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
(deploy IAM user with `lambda:UpdateFunctionCode/Configuration`,
`lambda:PublishLayerVersion`, `lambda:GetFunction`). See
`../../jobpilot-frontend/infra/README.md` for the full cross-repo runbook +
the Cloudflare DNS records.

## Cost allocation tags

All resources are tagged `Project=jobpilot` / `Environment=prod` (+ `ManagedBy`,
`Component`) via provider `default_tags`. Activate `Project` and `Environment`
as cost-allocation tags once in the AWS Billing console to filter costs by them.

## Redeploy manually (outside CI)

```bash
bash scripts/build_layer.sh && bash scripts/package.sh
cd infra/terraform && terraform apply    # first time / infra changes only
# code-only redeploys go through CI, or: aws lambda update-function-code ...
```

## Notes
- **resume** has the highest memory/timeout (long async LLM work) and is the only
  function that self-invokes for the async-job branch (IAM `SelfInvoke` covers it).
- The 4 functions share one execution role (`iam.tf`). Tighten per-service if needed.
- For shared remote state, configure the S3 backend in `versions.tf`.
- `*.tfvars` and `build/` / `*.zip` are gitignored — never commit secrets or artifacts.
