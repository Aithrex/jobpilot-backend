# JobPilot Backend

Flask API for the **Aspirely** AI career platform (`jobpilot` is the repo/internal name) — MongoDB + **Claude-on-Bedrock**, deployed to **AWS Lambda**. The frontend is a **separate repo** (`jobpilot-frontend`).

**5 deployed Lambdas** under `services/<svc>/`: four HTTP services (**auth · jobs · resume · billing**), each with its own `app.py` (Flask factory), `lambda_handler.py` and `blueprints/`; plus **`apply_executor`**, a headless container-image Lambda with **no HTTP routes** (invoked Lambda-to-Lambda). Shared business logic + framework live in the **`shared/python` Lambda layer** (mounted `/opt/python`). `from services.X` / `from blueprints.X` resolve across a service's local dir AND the shared layer via **PEP 420 namespace packages** (so NO `__init__.py` in any `services/`/`blueprints/` dir — adding one breaks the merge). `local.py` stitches the four HTTP services into one Flask app for local dev.

> **`services/learning/` is a NOT-YET-DEPLOYED skeleton.** It is absent from `var.service_config` in `infra/terraform/variables.tf`, and both `lambda.tf` and `apigateway.tf` `for_each` over that map — so no learning Lambda and no `/api/learning` edge route exist. `local.py` doesn't register it either. **Live traffic is served by `resume` at `/api/resume/learning/*`**; the skeleton exists to make the domain one-route extractable later (see its `app.py` docstring). Deploying it = add a `learning` entry to `service_config` + the deploy workflow's path filter.

## Commands (run from this directory)
- `python local.py` — local gateway: runs the four HTTP services as ONE Flask app on port 5000. Health: `GET /api/health`. **Needs MongoDB + AWS creds / Bedrock access** (without AWS creds, DB/search/career-pages work but every AI action fails).
- `PYTHONPATH=shared/python pytest tests evals -q` — **the offline test + eval suite** (~117 test modules + 11 evals). Fully deterministic: **mongomock** for Mongo, a **FakeLLM** for Bedrock — no AWS creds, no secrets, no network. This is exactly what `.github/workflows/tests.yml` runs. Single test: append `tests/test_foo.py::test_name`.
- `pip install -r requirements-dev.txt` — pytest + mongomock + openpyxl (dev/CI only; **not** installed by the runtime requirements, so a fresh venv has no pytest).
- `pip install -r shared/python/requirements.txt` — layer deps (Flask/pymongo/boto3/google-genai/fpdf2/docx/pypdf/crypto/PyJWT/lxml/requests…). **All runtime deps live here** — there are no per-service requirements files.
- `python scripts/check_bedrock.py` — verify Bedrock creds + Claude model access (run before first use).

## Hard invariants (breaking these breaks login, billing, AI integrity, or prod)
1. **Auth error contract:** auth FAILURES return **400/409/403/429 — NEVER 401** (401 = `@jwt_required()` missing/bad token only; the FE force-logs-out on **any** 401). Auth success payloads are **flat**; reset uses **`new_password`**. JWT identity = email; **`JWT_SECRET_KEY` must match the legacy portfolio value** — don't rotate.
2. **Long work = async job, never inline:** `get_resume_service().create_job(type,payload,user_email)` + `ResumeService.invoke_async(...)` → `{job_id}`; add an `elif job_type==…` branch in `shared/python/services/resume_service.py::_process_job`; client polls `GET /api/resume/job/<id>` (state in `resume_jobs`). Dispatch = Lambda self-invocation.
3. **Gate before you spend:** `entitlements_service.require_feature(key)` (→403 `upgrade_required`) / `check_and_increment(email,feature)` **before** any LLM call. Tailor quota = `usage_service`/`tailor_usage` (don't double-count).
4. **Sanitize all untrusted input** with `shared/python/utils/security.py::InputSanitizer` before **any** Mongo filter (NoSQL injection). Never place user input in an operator position. Use atomic `$inc` for counters (concurrent Lambdas).
5. **AI only via the provider abstraction:** `services/claude_client.py` is the facade **all** services call (`claude_json`, `CLAUDE_FLASH/PRO/PREVIEW`) — never call boto3 Bedrock (or google-genai) directly from routes/services. Under it, `services/llm_providers/` selects at runtime on **`LLM_PROVIDER`** (SSM in prod, env locally): **`claude`** = Claude on AWS Bedrock via boto3 `invoke_model`, `anthropic_version: bedrock-2023-05-31`, **no Anthropic SDK** (default) · **`gemini`** = Google Gemini via `google-genai`. The public surface is provider-agnostic, so switching touches no call site. Tiers: **FLASH = Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, ~8k out) for extraction; **PRO = Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6`, 16k+) for tailoring/JD/scoring. **Never fabricate résumé facts** — `integrity_guard.py` + `fabrication_filter.py` enforce this; never weaken them. LLM output is schema-validated (`schemas/resume_schemas.py`) + repaired (`llm_providers/_json_repair.py`, max 6 rounds/25s).
6. **New `/api/<prefix>`** needs its prefix added to the owning service's `routes` list in **`var.service_config`** (`infra/terraform/variables.tf`) — `apigateway.tf` and `lambda.tf` both `for_each` over that map, registering `ANY <prefix>` + `ANY <prefix>/{proxy+}`. Not there ⇒ 404 in prod (this is exactly why `learning` is dark).
7. **Only `apply_executor` runs a real browser.** Never add Playwright/Chromium to another service — the others are zip Lambdas without the runtime.

## Stack
Flask (per-service factory `services/<svc>/app.py::create_app()`) · MongoDB `aspirely_db` via **raw PyMongo** (NO ORM, NO migration framework, **NO indexes defined in-repo → most queries are COLLSCAN**) · Claude on AWS Bedrock (or Gemini) · AWS Lambda (5× zip + 1× container `lambda_handler.py` + apig-wsgi) / local `local.py` gateway · PyJWT + flask-jwt-extended · bcrypt (12 rounds) · boto3 (S3/SES/SSM/Bedrock/Lambda) · Playwright + Chromium (`apply_executor` only) · fpdf2/python-docx/pypdf · cryptography. Tests: pytest + mongomock + FakeLLM.

## Architecture (thin blueprints → fat shared services → shared layer)
Each `services/<svc>/` is its own deployable: `app.py` (factory, registers only its blueprints + CORS/JWT/security-header hook), `lambda_handler.py` (apig-wsgi; **only `resume` has the async-job self-invoke branch**), `blueprints/`, and optionally a local `services/` of domain-only logic. `package.sh` bundles only `services/<svc>/` code — **a module a Lambda needs but doesn't own must live in the shared layer.**

| Lambda | Blueprints | Routes | Edge |
|---|---|---|---|
| **auth** | `auth` (JWT mint, OAuth, reset), `admin` (`@admin_required`) | ~40 | `ANY /api/auth/{proxy+}`, `ANY /api/admin/{proxy+}` |
| **jobs** | `jobs`, `apply` (extension) | ~58 | `ANY /api/jobs/{proxy+}`, `ANY /api/apply/{proxy+}` |
| **resume** | `resume`, `executor`, `guest`, `mailbox`, `mail_inbound`, `sprint_hub`, `voice_graph`, `tech_chronicle`, `feedback` | **~205** | `ANY /api/resume/{proxy+}`, `ANY /api/tech-chronicle/{proxy+}` |
| **billing** | `billing` (entitlements surface) | ~6 | `ANY /api/billing/{proxy+}` |
| **apply_executor** | — none — | 0 | not routed; invoked by `resume` |
| *(learning)* | `learning` | ~9 | **not deployed** — see the note above |

Every `resume` blueprint except `tech_chronicle` mounts under **`/api/resume/*`** (`feedback` → `/api/resume/feedback`, `voice_graph` → `/api/resume/voice-graph`, `sprint_hub` → `/api/resume/hub`, `guest` → `/api/resume/guest`, `mailbox` → `/api/resume/mailbox`, `mail_inbound` → `/api/resume/mail-inbound`, `executor` → `/api/resume/executor`) — which is why the service needs only two edge prefixes. Note `local.py` registers a **subset**: `mailbox`, `mail_inbound` and `executor` are prod-only, so those routes are absent from the local gateway.

**`resume` is the hub** (~205 of ~318 routes): tailoring, ATS, copilot, interviews, learning, voice, managed mailbox, guest flow — plus 12 local services (career-copilot, interview-coach, overview, career-readiness, job-intelligence, beta-features, recommendations, streak, github_*, linkedin_*).

**`apply_executor`** ([services/apply_executor/lambda_handler.py](services/apply_executor/lambda_handler.py)) is the auto-apply browser fleet: a Playwright/Chromium container image (~2 GB, 300 s, small reserved concurrency as a cost + politeness cap). `resume` reaches it via `LambdaInvokeExecutor` with `{"method","args"}` → dispatched to `shared/python/services/browser_executor.py::BrowserExecutor` (`recon_form`, `create_account`, `submit`, `verify_state`, `is_available`). It swallows exceptions into clean contract shapes — a bad call reads as unavailable/failed, never a stack trace.

**`shared/python/`** = the Lambda layer, shared by all 6: **~126 services** (incl. `resume_service` async orchestrator + its parser/tailor/scorer/renderer tree, `job_service`, `browser_executor`, `entitlements_service`, `usage_service`, `s3_service`, `gmail_service`, `claude_client`, `llm_providers/`, the job-source catalogs), `schemas/resume_schemas.py` (LLM output: PARSED/TAILORED_RESUME, JD_ANALYSIS, ATS_SCORES), `utils/` (`db_connect` Mongo singleton, `config` +SSM, `security`), `models/user.py` (pw rules: 8+ chars, upper/lower/digit/special, bcrypt), `requirements.txt`.

> **ONE match score:** `shared/python/services/fit_score.py` (ADR-002, `fit-v2.1`) is the **only** candidate-to-job match score. `/api/jobs/search`, `/api/jobs/pipeline/match-scores`, the inventory feed, the career-portal scans and the `apply_match_sweep` gate (band "good match" = **65**) all read it. Never add a second formula and never post-process its output under the same `match_score` key — carry ordering/preference signals in `rank_score` and tailorability in `tailor_feasibility`. **No parsed résumé ⇒ NO score** (`match_score = 0`, no `fit`/`score_version` marker → the FE renders "Not scored"); never a number.

> **Coupling note:** `jobs.py` uses `resume_service` and `resume_service` uses `job_service` — both live in the shared layer precisely because the jobs↔resume domains are circularly coupled. Keep cross-domain services in `shared/python/services/`; a shared service must NOT import a domain-local one.

## Employer identity and career-source ownership (ADR-006, 2026-09-02)

Two NEW collections, both additive and both reversible by dropping them.
`companies`, `company_ats_endpoints` and `job_postings` are untouched, so the
crawler reads exactly the rows it read before.

- **`employers`** (`shared/python/services/employer_registry.py`) — ONE
  canonical `employer_id` (`emp_<12 hex>`) shared by `companies`, the curated
  seed, the three Python catalogs, `company_ats_endpoints` and `job_postings`.
  Derived deterministically, domain-first: `sha1("domain:"+registrable_domain)`
  > `sha1("curated:"+key)` > `sha1("name:"+name_key)`, with
  `identity_confidence` 0.95 / 0.80 / 0.45. **Never merge two employers on a
  normalized name** — `merge_decision` returns `merge` only on a shared
  verified domain, a curated alias, or an owner-verified `(vendor, tenant)`;
  everything else that merely looks like a match is `quarantine` and goes to
  `employer_merge_reviews`. `operating_status` (`active|renamed|acquired|
  merged|closed|product_or_brand`) is load-bearing: the resolver FOLLOWS
  `parent_employer_id`, so an acquisition never deletes an employer from the
  denominator. `IDENTITY_OVERRIDES` repairs the documented conflicts (Stripe,
  Notion, Ramp, Capital One, Intuit, JPMorgan, Boeing, Roche, AT&T, Abbott,
  NVIDIA, Salesforce/Slack, Alphabet/BMW).
- **`career_sources`** (`career_source_service.py` + `career_source_ownership.py`)
  — a nine-state machine (`candidate, ownership_verified, content_validated,
  validated, empty, blocked, moved, dead, manual_review`). **Only `validated`
  and `empty` count as coverage.** Ownership needs EITHER a link/redirect
  chain from an independently verified employer domain to the exact endpoint,
  OR two corroborating signals from two DIFFERENT classes
  (`employer_domain` / `ats_provider` / `curated_registry`).
  `SIGNAL_SLUG_SIMILARITY` and `SIGNAL_ENDPOINT_RESPONDS` are recorded and
  **never counted** — that is the whole fix.

> **`company_ats_endpoints.status == "validated"` is NOT coverage.** It means
> the endpoint answered. It cannot tell a company's own board from a
> stranger's board with a similar slug, and it is the number that reported
> 43.0%. Coverage now comes from `career_sources`. Both are printed side by
> side by the report so the gap is visible.

Scripts, all **dry-run by default**:
- `scripts/backfill_employer_registry.py [--apply]` — builds `employers` +
  `career_sources` from all five registries.
- `scripts/verify_career_sources.py [--apply] --mode endpoints|employers|both`
  — pass 1 asks the ATS who owns the board (cheap); pass 2 verifies the
  employer's domain and walks from it to the board (decisive, and the only
  path that reaches Workday/iCIMS/Oracle, which name no owner).
- `scripts/employer_coverage_report.py [--markdown]` — THE accountability
  artifact. Read-only. Generates `docs/coverage/tech-employer-coverage.md`
  stamped with the source revision and the inventory watermark.

## Job sourcing
Jobs come from JSearch **plus direct employer ATS portals** — the differentiator. Catalogs + engines live in the shared layer: `workday_tenant_catalog.py` / `workday_jobs_service.py` / `workday_cxs_fetcher.py` (Workday CXS) · `career_pages_catalog.py` / `career_pages_service.py` (**`FETCHERS`** greenhouse/lever/ashby/smartrecruiters/workable/recruitee; **`ENTERPRISE_FETCHERS`** amazon/eightfold/phenom/orc/uber) · `mnc_catalog.py` / `mnc_jobs_service.py`. Routes in `services/jobs/blueprints/jobs.py` (`/workday/*`, `/career-pages/*`, `/mnc/*`). Adding companies = catalog rows, validated live via `scripts/discover_workday_tenants.py` / `scripts/discover_career_pages.py`; a new ATS = a `_fetch_<ats>()` registered in `FETCHERS`. **Apify is disabled** (reversible — every edit tagged `APIFY DISABLED (temporary)`).

## Occupation taxonomy and match scoring (`tech-tax-1.1.0`, `fit-v2.1`)

`shared/python/services/tech_taxonomy.py` is the **canonical occupation
ontology** — 14 families / 102 specialties — and it is now what ingestion,
résumé parsing, search AND `fit_score` all score against. `fit_score.
ROLE_FAMILIES` (32 coarse keys) survives only as the LEGACY BRIDGE for
`job_postings.role_family`, which three production indexes key on; nothing in
the scoring path reads it for role alignment any more.

- **`semiconductor_hardware` is CORE**, not adjacent. It was adjacent, which
  meant the default scope hid every ASIC, RTL, verification, firmware, analog,
  RF, EDA and photonics role unless the searcher ticked an extra box.
- **Role alignment is a five-rung ladder** (`role_alignment`): `exact_specialty
  > adjacent_specialty > same_family > adjacent_family > unrelated`, over a
  versioned, DECLARED adjacency matrix (`ADJACENCY_VERSION`, 184 pairs). The
  previous implementation's finest software distinction was the single family
  `software_engineering`, so for anyone in software this 25-point component was
  near-constant and the whole score distribution compressed.
- **`unknown` is a sixth answer and is off the ladder.** Not knowing costs
  what it always cost; it must never cost what being wrong costs.
- **Unknown is EXCLUDED, not priced.** `_normalized_score` averages only the
  components with evidence on BOTH sides. Every component used to pay a
  "neutral" fraction when it had nothing to say, and a neutral fraction is
  still a deduction — on live data that was a systematic ~12-point tax on
  every candidate for gaps in OUR parsing (96.1% of postings state no work
  mode, 43.3% no seniority).
- **`eligibility` is separate from `score`** and is not a second score:
  `eligible | ineligible | not_scored`, fired only on a KNOWN mismatch — a
  confident cross-domain verdict, a named licence the résumé lacks, or a
  stated sponsorship conflict. Silence is never a mismatch; an unstated
  clearance is `unknown` and surfaces as a requirement to read, not a penalty.
- **`cross_domain` means a change of OCCUPATIONAL DOMAIN, never an unfamiliar
  technology family.** It is the only thing that turns role alignment into an
  INELIGIBLE verdict, and ineligible rows leave the ranked feed entirely. Two
  technology families the adjacency matrix does not connect score `unrelated`
  (0.08 — already a severe penalty) and stay eligible. Setting it on any
  unrelated tech pair made a software-and-cloud résumé ineligible for 522 of
  2,500 live core-tech postings, 478 of them security or staff/architect roles
  (`docs/coverage/fit-score-v21-distribution.md`).
- **`tech-adjacency-1.1.0`** adds the edges leadership and security are
  actually staffed from: IC specialties → `principal_staff_ic` /
  `architecture`, software → `application_security` / `product_security`,
  cloud → `iam_pam` / `cloud_security` / `secops`. `engineering_management`
  keeps no direct IC pair on purpose — it is reachable at the adjacent-family
  rung through the bridge, which is the honest distance.
- **Bumping `FIT_SCORE_VERSION` is safe now**, via
  `_COMPATIBLE_REQUIREMENT_VERSIONS`: the extraction contract is unchanged, so
  216,687 stored `requirements` profiles stay valid through the deploy.
- **`principal` is its own seniority band**, between `staff` and `director`.
  The feed accepted it as a filter and `job_postings.seniority` could never
  hold it.

Backfills: `scripts/backfill_tech_taxonomy.py --apply` (version-aware — it
selects rows whose `tech.taxonomy_version` is not current, so a bump is
backfillable without `--all`) and `scripts/backfill_posting_normalization.py
[--apply] [--with-bodies]`, which brings stored postings up to
`NORMALIZATION_VERSION` from fields already on the document. The second one is
how 11,653 postings that stated "minimum N years" but read `seniority:
unknown` got their band back — retrieval filters on the stored value, so those
rows were invisible to anyone filtering by level. **It stamps the version only
when JD bodies were actually read** (`--with-bodies`, from our own S3):
`job_ingest_worker._listing_implies_same_content` treats the stamp as "needs
no re-fetch", so stamping a half-upgraded row would freeze it there.

Evals: `evals/eval_tech_taxonomy.py` (per-family AND per-stratum precision/
recall; stratum **H** is titles taken verbatim from live `job_postings`) and
`evals/eval_tech_feed_retrieval.py` + `evals/test_tech_feed_retrieval.py`
(retrieval by source / family / specialty / cohort / ambiguous title /
geography / seniority, plus the traversal-ceiling regression).

## Retrieval: no 2,000-result ceiling

`tech_feed_service` pages in two phases. `_serve_page` replays the frozen,
personalised RANKED order (bounded by `MAX_ORDERED`, because it lives in a
session document). When that is exhausted the cursor CHANGES SHAPE to a
keyset — `{"s": id, "k": {"p": posted_at, "j": job_id}}` — and
`_serve_keyset_page` continues in `(posted_at DESC, job_id ASC)` index order
over every matching posting. Ordering stays total and stable, dedup still
runs, cursor expiry still 410s, and the response says `sort: "index_order"`
so a client can tell it is no longer relevance-ranked.

`total` is now the real match count (bounded by `COUNT_LIMIT`), not the length
of the ranked window.

## Config & data
Config via `shared/python/utils/config.py`; secrets via **AWS SSM** in prod (`USE_SSM_SECRETS=true`), `.env` locally (see `.env.example`). Résumé collection is **`user_resumes`** (not `base_resumes`). Résumé files → **S3**. Keep cold starts lean: reuse boto3/Mongo clients at module scope, no heavy import-time work. Deploy lives in-repo: `infra/terraform/` (HTTP API + the 6 Lambdas + shared layer + IAM + KMS credential store + managed mailbox/SES + schedulers) and `scripts/build_layer.sh` + `scripts/package.sh`; see `infra/README.md`. CI: `.github/workflows/tests.yml` (offline suite) + `deploy.yml` (path-filtered per-service deploy).

## Docs
`docs/` is current and worth reading before touching a feature: `specs/` (auto-apply fill engine, managed mailbox, guest onboarding, resume-platform-v3) · `security/` (auto-apply threat model, dataflow, audit, ADR) · `ops/` (auto-apply runbook, observability, rollout/rollback, mailbox + executor setup) · `qa/auto-apply-acceptance.md` · `decisions/` (ADR-001..003) · `workday-tenant-expansion.md` + `career-pages-expansion.md` (validated, not-yet-pasted catalog rows).

## Agents & skills
`.claude/agents/` holds a scoped subagent suite (`orchestrator`, `backend-engineer`, `database-engineer`, `data-scientist`, `security-engineer`, `performance-engineer`, `staff-engineer`, `qa-test-engineer`, `devops-engineer`). Start with **`orchestrator`** for fuzzy asks; see `.claude/agents/README.md`. `.claude/skills/` adds `api-design-principles`, `python-testing-patterns`.
