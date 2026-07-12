# JobPilot — Backend

> API service for JobPilot: resume tailoring, job search, application autofill,
> interview feedback, learning, and tech-chronicle. Part of the
> [Aithrex](https://aithrex.tech) suite.

Flask API on **MongoDB** + **Claude on AWS Bedrock**, deployed as **4 independent
AWS Lambda microservices** (auth · jobs · resume · billing) behind one HTTP API,
all sharing one **Lambda layer** of common code + deps. The frontend is a
separate repo (`jobpilot-frontend`) and talks to this only over `/api/*`.

## Layout

```
services/<svc>/       one deployable Lambda each (auth · jobs · resume · billing[ · learning])
  app.py              Flask app factory — create_app() (registers its blueprints, CORS, JWT)
  lambda_handler.py   Lambda entrypoint (API Gateway → apig-wsgi → Flask)
  blueprints/         the service's HTTP routes
  services/           service-local domain logic (auth/jobs/resume only)
shared/python/        the Lambda LAYER (mounted /opt/python) — shared by all 4:
  services/           cross-domain business logic (resume_service, job_service, llm_providers, …)
  utils/ models/ schemas/ config/
  requirements.txt    ← the ONLY runtime dependency list (feeds the layer)
local.py              stitches all services into ONE Flask app for local dev (dev-server entry)
infra/                Terraform (HTTP API + 4 Lambdas + layer + IAM) + build scripts
requirements.txt      local-dev install target → `-r shared/python/requirements.txt`
requirements-dev.txt  test/CI only (pytest, mongomock, openpyxl)
```

> **Dependencies:** services carry **no per-service `requirements.txt`** — every
> runtime dep lives in `shared/python/requirements.txt` (bundled into the layer;
> `scripts/package.sh` ships only code). Add a runtime dep there or it's absent
> in prod.

## Auth model

JWT identity = email; **`JWT_SECRET_KEY` must match the legacy portfolio value**
(don't rotate). Auth *failures* return **400/409/403/429 — never 401** (401 is
reserved for a missing/bad token on a `@jwt_required()` route; the frontend
force-logs-out on any 401). See `.env.example` for the full secret list.

## Local development

Runs all services as one Flask app on **port 5000** (`PORT` to override).
**Needs MongoDB + AWS creds / Bedrock access** for the AI endpoints.

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt          # = shared/python/requirements.txt
pip install -r requirements-dev.txt      # optional: to run the test suite
cp .env.example .env                      # then fill in JWT_SECRET_KEY, MONGODB_URI, keys
python local.py                           # → http://localhost:5000/api/*  (health: /api/health)

python scripts/check_bedrock.py           # verify Bedrock creds + Claude model access
```

## Tests

Fully offline — mongomock for Mongo, a FakeLLM for Bedrock; no AWS creds, no network.

```bash
pip install -r shared/python/requirements.txt -r requirements-dev.txt
PYTHONPATH=shared/python pytest tests evals -q
```

## Deployment

Build → package → Terraform. Full runbook in [`infra/README.md`](infra/README.md).

```bash
bash scripts/build_layer.sh        # -> build/layer.zip  (shared code + deps, Linux wheels)
bash scripts/package.sh            # -> build/{auth,jobs,resume,billing}.zip  (code only)
cd infra/terraform && terraform apply   # first time / infra changes
```

Prod reads secrets from **AWS SSM** (`USE_SSM_SECRETS=true`). CI/CD:
`.github/workflows/deploy.yml` auto-deploys on push to `main` (rebuilds the layer
on `shared/**` changes → redeploys all 4; otherwise ships only changed services).
The HTTP API fans `/api/<prefix>` to the right Lambda — **any new top-level
`/api/<prefix>` needs a matching Terraform edge route or it 404s in prod**.

| Prefix | Lambda |
|---|---|
| `/api/auth`, `/api/admin` | auth |
| `/api/jobs`, `/api/apply` | jobs |
| `/api/resume` (+ `/api/resume/feedback`), `/api/tech-chronicle` | resume |
| `/api/billing` | billing |
