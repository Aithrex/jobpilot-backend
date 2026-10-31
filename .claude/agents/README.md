> **Hosting changed 2026-10-31.** JobPilot now runs on one Oracle Cloud box in Docker Compose, not AWS. Lambda, API Gateway, CloudFront, SSM, SES, KMS and Bedrock details below are history. Current setup: `infra/compose/README.md` in `jobpilot-backend`.

# JobPilot Backend — Agent Architecture

Codebase-aware Claude Code subagents for `jobpilot-backend` (Flask · MongoDB/PyMongo · Claude-on-Bedrock · AWS Lambda). Each agent encodes this repo's real stack and **hard invariants**, operates at a senior bar, and knows when to hand off. Use this set when you're working **inside the backend repo**. (The platform-wide hub spanning both repos lives in `D:\Aithrex\.claude\agents\`.)

## The roster

| Agent | Role | Writes code? | Use for |
|---|---|---|---|
| **orchestrator** | Prompt-refiner & router | No (read-only) | Vague/multi-part requests — refines intent, routes to specialists |
| **backend-engineer** | Flask/Python feature builder | ✅ | Blueprints, services, async jobs, integrations, entitlements wiring |
| **database-engineer** | MongoDB / PyMongo | ✅ | Data modeling, queries, indexing, atomic updates, migrations |
| **data-scientist** | Applied AI (Claude-on-Bedrock) | ✅ | Prompts, model tiers, schemas, anti-hallucination, ATS scoring, evals |
| **security-engineer** | AppSec | ✅ (surgical) | JWT/auth, NoSQL injection, secrets, CORS, SSRF, OAuth, PII |
| **performance-engineer** | Speed & cost / "time-efficiency" | ✅ (surgical) | Cold starts, query latency, LLM cost, caching, payloads |
| **staff-engineer** | Architect & reviewer (SDE) | No (read-only) | Design decisions, code review, service boundaries, tech-debt |
| **qa-test-engineer** | Test strategy & authoring | ✅ | Stand up pytest, mock AWS/Mongo/Bedrock, LLM evals |
| **devops-engineer** | Lambda / platform | ✅ | Packaging, shared layer, SSM, IAM, cold-start tuning, edge routing |

> No `frontend-engineer` or `uiux-designer` here — UI lives in the `jobpilot-frontend` repo.

## How to use

- **Auto-delegation**: describe your task; Claude Code routes via each agent's `description`.
- **Explicit**: "Use the **data-scientist** to reduce hallucination in the tailor step."
- **Recommended entry point for anything fuzzy**: start with **orchestrator** — it refines the ask and returns a step plan naming which specialists to run, with ready-to-paste sub-prompts.

## Typical flows

- **New AI feature**: `orchestrator` → `staff-engineer` (design) → `data-scientist` (prompt/schema/tier) + `backend-engineer` (async-job wiring) + `database-engineer` (storage) → `qa-test-engineer` (eval) → `security-engineer`/`performance-engineer` (review).
- **New endpoint**: `backend-engineer` → `database-engineer` (if new data) → `security-engineer` → `qa-test-engineer`.
- **"Tailoring is slow / Bedrock bill is high"**: `performance-engineer` (+ `data-scientist` on tier/tokens).
- **"It works locally but 404s in prod"**: `devops-engineer` (edge routing).
- **Pre-merge review**: `staff-engineer` → `security-engineer` → `performance-engineer`.

## Conventions baked into every agent

- **Permissions**: builders (`backend-engineer`, `database-engineer`, `data-scientist`, `qa-test-engineer`, `devops-engineer`) and surgical fixers (`security`, `performance`) can edit; **`orchestrator` and `staff-engineer` are read-only** (they plan/route/review). Tune in each file's `tools:` line.
- **Model**: every agent is `model: inherit`. Override per-agent if desired (e.g. `opus` for `staff-engineer`/`data-scientist`).
- **Invariants enforced everywhere** (violating these breaks login, billing, AI integrity, or prod):
  1. **Auth error contract** — auth *failures* return **400/409/403/429, NEVER 401** (401 = `@jwt_required` missing/bad token only; FE force-logs-out on any 401). Auth success payloads are **flat**; reset field is `new_password`. JWT identity = email; `JWT_SECRET_KEY` must match the legacy portfolio value.
  2. **Long work = async job** — `create_job` → `{job_id}` → extend `_process_job` (`elif job_type==…`) → client polls `/api/resume/job/<id>`. Never inline.
  3. **Gate spend** — `entitlements_service.require_feature`/`check_and_increment` **before** any LLM call; tailor quota in `usage_service`/`tailor_usage` (no double-count).
  4. **AI only via the provider abstraction** (`services/llm_providers.get_provider()`; legacy `claude_client` facade → Claude). Tier: Haiku 4.5 (FLASH, 8k) for extraction, Sonnet 4.6 (PRO, 16k+) for tailoring/scoring. **Never fabricate resume facts** — IntegrityGuard enforces this.
  5. **Sanitize untrusted input** (`InputSanitizer`) before every Mongo filter; atomic `$inc` for counters.
  6. **New `/api/<prefix>`** needs an external (Terraform, not in this repo) edge route or it 404s in prod.

## Stack quick-reference
App factory `app.py::create_app()` · Lambda `lambda_handler.py` (apig-wsgi) · local `local.py` · Mongo singleton `shared/python/utils/db_connect.py` (`aspirely_db`) · config/secrets `shared/python/utils/config.py` + SSM · security `shared/python/utils/security.py` · LLM `services/llm_providers/claude.py` · async orchestrator `services/resume_service.py` · entitlements `services/entitlements_service.py` · schemas `schemas/resume_schemas.py`.

## Editing the agents
Each file is Markdown with YAML frontmatter (`name`, `description`, `tools`, `model`) + a system prompt. Edit freely; keep `description` trigger-rich (it drives routing) and keep the invariants accurate as the codebase evolves.
