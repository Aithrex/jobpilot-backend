# JobPilot Backend

Flask API for the JobPilot AI career platform — MongoDB + **Claude-on-Bedrock**, deployed to **AWS Lambda**. The frontend is a **separate repo** (`jobpilot-frontend`).

## Commands (run from this directory)
- `python run_local.py` — local Flask dev server (port 5000; wires `shared/python` onto `sys.path`). **Needs MongoDB running + AWS creds / Bedrock access.**
- `python scripts/check_bedrock.py` — verify Bedrock creds + Claude model access (run before first use).
- `pip install -r requirements.txt` — service deps (framework/db/auth come from the **`shared/python` layer**).
- _No tests configured yet_ (pytest + mongomock + moto + a fake LLM provider recommended — see the `qa-test-engineer` agent).

## Hard invariants (breaking these breaks login, billing, AI integrity, or prod)
1. **Auth error contract:** auth FAILURES return **400/409/403/429 — NEVER 401** (401 = `@jwt_required()` missing/bad token only; the FE force-logs-out on **any** 401). Auth success payloads are **flat**; reset uses **`new_password`**. JWT identity = email; **`JWT_SECRET_KEY` must match the legacy portfolio value** — don't rotate.
2. **Long work = async job, never inline:** `get_resume_service().create_job(type,payload,user_email)` + `ResumeService.invoke_async(...)` → `{job_id}`; add an `elif job_type==…` branch in `services/resume_service.py::_process_job`; client polls `GET /api/resume/job/<id>` (state in `resume_jobs`). Dispatch = Lambda self-invocation.
3. **Gate before you spend:** `entitlements_service.require_feature(key)` (→403 `upgrade_required`) / `check_and_increment(email,feature)` **before** any LLM call. Tailor quota = `usage_service`/`tailor_usage` (don't double-count).
4. **Sanitize all untrusted input** with `shared/python/utils/security.py::InputSanitizer` before **any** Mongo filter (NoSQL injection). Never place user input in an operator position. Use atomic `$inc` for counters (concurrent Lambdas).
5. **AI only via the provider abstraction:** `from services.llm_providers import get_provider` (impl `services/llm_providers/claude.py` — Claude on Bedrock via boto3 `invoke_model`, `anthropic_version: bedrock-2023-05-31`, **no Anthropic SDK**). **`services/gemini_client.py` is a LEGACY FACADE that routes to Claude — there is NO Gemini call.** Never call boto3 Bedrock directly from routes/services. Tiers: **FLASH = Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, ~8k out) for extraction; **PRO = Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6`, 16k+) for tailoring/JD/scoring. **Never fabricate résumé facts** — `integrity_guard.py` + `fabrication_filter.py` enforce this; never weaken them. LLM output is schema-validated (`schemas/resume_schemas.py`) + repaired (`services/llm_providers/_json_repair.py`, max 6 rounds/25s).
6. **New `/api/<prefix>`** needs an external (Terraform) edge route or it 404s in prod (no `.tf` in this repo).

## Stack
Flask (factory `app.py::create_app()`) · MongoDB `portfolio_db` via **raw PyMongo** (NO ORM, NO migration framework, **NO indexes defined in-repo → most queries are COLLSCAN**) · Claude on AWS Bedrock · AWS Lambda (`lambda_handler.py` + apig-wsgi) / local `run_local.py` · PyJWT + flask-jwt-extended · bcrypt (12 rounds) · boto3 (S3/SES/SSM/Bedrock) · fpdf2/python-docx/pypdf · cryptography.

## Architecture (thin blueprints → fat services → shared layer)
- **`app.py`** — registers 8 blueprints under `/api/<x>`, CORS (`ALLOWED_ORIGINS`, credentials), JWT, security-header hook.
- **`blueprints/`** (routes, thin): `auth` (JWT mint, OAuth, reset), `resume` (core async APIs), `jobs`, `apply` (extension), `feedback`, `billing` (stub), `admin` (`@admin_required`, server `users.is_admin`), `tech_chronicle`.
- **`services/`** (logic, 45+): `resume_service.py` (async orchestrator), parser/tailor/scorer/renderer, `entitlements_service.py`, `usage_service.py`, `oauth_service.py`, `s3_service.py`, `email_service.py`, `gmail_service.py`, `llm_providers/`.
- **`schemas/resume_schemas.py`** — JSON schemas for LLM output (PARSED/TAILORED_RESUME, JD_ANALYSIS, ATS_SCORES).
- **`shared/python/`** (Lambda layer, mounted `/opt/python`): `utils/db_connect.py` (Mongo singleton), `utils/config.py` (+SSM), `utils/security.py`, `models/user.py` (password rules: 8+ chars, upper/lower/digit/special, bcrypt).

## Config & data
Config via `shared/python/utils/config.py`; secrets via **AWS SSM** in prod (`USE_SSM_SECRETS=true`), `.env` locally (see `.env.example`). Résumé collection is **`user_resumes`** (not `base_resumes`). Résumé files → **S3**. Keep cold starts lean: reuse boto3/Mongo clients at module scope, no heavy import-time work. The `apply` blueprint and a couple of prefixes have dedicated API Gateway routes.

## Agents
`.claude/agents/` holds a scoped subagent suite (`orchestrator`, `backend-engineer`, `database-engineer`, `data-scientist`, `security-engineer`, `performance-engineer`, `staff-engineer`, `qa-test-engineer`, `devops-engineer`). Start with **`orchestrator`** for fuzzy asks; see `.claude/agents/README.md`.
