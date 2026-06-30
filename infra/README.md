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

## Redeploy after code changes

```bash
bash scripts/build_layer.sh && bash scripts/package.sh   # only what changed
cd infra/terraform && terraform apply                    # source_code_hash picks up new zips
```

## Notes
- **resume** has the highest memory/timeout (long async LLM work) and is the only
  function that self-invokes for the async-job branch (IAM `SelfInvoke` covers it).
- The 4 functions share one execution role (`iam.tf`). Tighten per-service if needed.
- For shared remote state, configure the S3 backend in `versions.tf`.
- `*.tfvars` and `build/` / `*.zip` are gitignored — never commit secrets or artifacts.
