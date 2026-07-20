# JobPilot Backend

Flask API for the JobPilot AI career platform — MongoDB + **Claude-on-Bedrock**, deployed to **AWS Lambda**. The frontend is a **separate repo** (`jobpilot-frontend`).

Split into **4 independently-deployed Lambda microservices** under `services/<svc>/` (**auth · jobs · resume · billing**), each with its own `app.py` (Flask factory), `lambda_handler.py`, and `blueprints/`. Shared business logic + framework live in the **`shared/python` Lambda layer** (mounted `/opt/python`). `from services.X` / `from blueprints.X` resolve across a service's local dir AND the shared layer via **PEP 420 namespace packages** (so NO `__init__.py` in any `services/`/`blueprints/` dir — adding one breaks the merge). `local.py` stitches all four into one Flask app for local dev.

## Commands (run from this directory)
- `python local.py` — local gateway: runs all 4 services as ONE Flask app on port 5000; wires `shared/python` + each `services/<svc>` onto `sys.path`. **Needs MongoDB + AWS creds / Bedrock access.**
- `python scripts/check_bedrock.py` — verify Bedrock creds + Claude model access (run before first use).
- `pip install -r shared/python/requirements.txt` — layer deps (Flask/pymongo/boto3/fpdf2/docx/pypdf/crypto/PyJWT/lxml/requests…). Per-service `requirements.txt` hold only service-unique extras.
- _No tests configured yet_ (pytest + mongomock + moto + a fake LLM provider recommended — see the `qa-test-engineer` agent).

## Hard invariants (breaking these breaks login, billing, AI integrity, or prod)
1. **Auth error contract:** auth FAILURES return **400/409/403/429 — NEVER 401** (401 = `@jwt_required()` missing/bad token only; the FE force-logs-out on **any** 401). Auth success payloads are **flat**; reset uses **`new_password`**. JWT identity = email; **`JWT_SECRET_KEY` must match the legacy portfolio value** — don't rotate.
2. **Long work = async job, never inline:** `get_resume_service().create_job(type,payload,user_email)` + `ResumeService.invoke_async(...)` → `{job_id}`; add an `elif job_type==…` branch in `services/resume_service.py::_process_job`; client polls `GET /api/resume/job/<id>` (state in `resume_jobs`). Dispatch = Lambda self-invocation.
3. **Gate before you spend:** `entitlements_service.require_feature(key)` (→403 `upgrade_required`) / `check_and_increment(email,feature)` **before** any LLM call. Tailor quota = `usage_service`/`tailor_usage` (don't double-count).
4. **Sanitize all untrusted input** with `shared/python/utils/security.py::InputSanitizer` before **any** Mongo filter (NoSQL injection). Never place user input in an operator position. Use atomic `$inc` for counters (concurrent Lambdas).
5. **AI only via the provider abstraction:** `from services.llm_providers import get_provider` (impl `services/llm_providers/claude.py` — Claude on Bedrock via boto3 `invoke_model`, `anthropic_version: bedrock-2023-05-31`, **no Anthropic SDK**). **`services/claude_client.py` is the facade all services call (`claude_json`, `CLAUDE_FLASH/PRO/PREVIEW`).** Never call boto3 Bedrock directly from routes/services. Tiers: **FLASH = Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, ~8k out) for extraction; **PRO = Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6`, 16k+) for tailoring/JD/scoring. **Never fabricate résumé facts** — `integrity_guard.py` + `fabrication_filter.py` enforce this; never weaken them. LLM output is schema-validated (`schemas/resume_schemas.py`) + repaired (`services/llm_providers/_json_repair.py`, max 6 rounds/25s).
6. **New `/api/<prefix>`** needs an external (Terraform) edge route or it 404s in prod (no `.tf` in this repo).

## Stack
Flask (per-service factory `services/<svc>/app.py::create_app()`) · MongoDB `aspirely_db` via **raw PyMongo** (NO ORM, NO migration framework, **NO indexes defined in-repo → most queries are COLLSCAN**) · Claude on AWS Bedrock · AWS Lambda (4× `services/<svc>/lambda_handler.py` + apig-wsgi) / local `local.py` gateway · PyJWT + flask-jwt-extended · bcrypt (12 rounds) · boto3 (S3/SES/SSM/Bedrock) · fpdf2/python-docx/pypdf · cryptography.

## Architecture (4 Lambdas · thin blueprints → fat shared services → shared layer)
Each `services/<svc>/` is its own deployable Lambda: `app.py` (factory, registers only its blueprints + CORS/JWT/security-header hook), `lambda_handler.py` (apig-wsgi; **only `resume` has the async-job self-invoke branch**), `blueprints/`, and (auth/jobs/resume) a local `services/` of domain-only logic. **No per-service `requirements.txt`** — all runtime deps live in the shared layer (`shared/python/requirements.txt`); `package.sh` bundles only code.
- **auth** → blueprints `auth` (JWT mint, OAuth, reset), `admin` (`@admin_required`); local services `oauth_service`, `email_service`. Edge: `ANY /api/auth/{proxy+}`, `ANY /api/admin/{proxy+}`.
- **jobs** → blueprints `jobs`, `apply` (extension); local services `jd_fetcher`, `jd_match_scorer`, `filter_suggester_service`, `answer_generator`. Edge: `ANY /api/jobs/{proxy+}`, `ANY /api/apply/{proxy+}`.
- **resume** → blueprints `resume` (core async APIs), `feedback` (**mounted `/api/resume/feedback`** to ride the resume route — FE calls it there), `tech_chronicle`; local services: career-copilot, interview-coach, overview, career-readiness, job-intelligence, beta-features, recommendations, streak, github_*, linkedin_*. Edge: `ANY /api/resume/{proxy+}`, `ANY /api/tech-chronicle/{proxy+}`.
- **billing** → blueprint `billing` (entitlements surface). Edge: `ANY /api/billing/{proxy+}`.
- **`shared/python/`** = the Lambda layer (mounted `/opt/python`), shared by all 4: `services/` (33 cross-domain services incl. `resume_service` async orchestrator + its parser/tailor/scorer/renderer tree, `job_service`, `entitlements_service`, `usage_service`, `s3_service`, `gmail_service`, `claude_client`, `llm_providers/`), `schemas/resume_schemas.py` (LLM output: PARSED/TAILORED_RESUME, JD_ANALYSIS, ATS_SCORES), `utils/` (`db_connect` Mongo singleton, `config` +SSM, `security`), `models/user.py` (pw rules: 8+ chars, upper/lower/digit/special, bcrypt), `requirements.txt`.

> **Coupling note:** `jobs.py` uses `resume_service` and `resume_service` uses `job_service` — both live in the shared layer precisely because the jobs↔resume domains are circularly coupled. Keep cross-domain services in `shared/python/services/`; a shared service must NOT import a domain-local one.

## Config & data
Config via `shared/python/utils/config.py`; secrets via **AWS SSM** in prod (`USE_SSM_SECRETS=true`), `.env` locally (see `.env.example`). Résumé collection is **`user_resumes`** (not `base_resumes`). Résumé files → **S3**. Keep cold starts lean: reuse boto3/Mongo clients at module scope, no heavy import-time work. **Every top-level `/api/<prefix>` needs a matching Terraform route to the right Lambda** (the 4-way split means routing now fans out per service — see the edge routes per service above). The deploy path now lives **in-repo**: `infra/terraform/` (HTTP API + 4 Lambdas + shared layer + IAM) and `scripts/build_layer.sh` + `scripts/package.sh`; see `infra/README.md`. (A separate external edge — CloudFront `/api/*` → this API — may still front prod.)

## Agents
`.claude/agents/` holds a scoped subagent suite (`orchestrator`, `backend-engineer`, `database-engineer`, `data-scientist`, `security-engineer`, `performance-engineer`, `staff-engineer`, `qa-test-engineer`, `devops-engineer`). Start with **`orchestrator`** for fuzzy asks; see `.claude/agents/README.md`.
